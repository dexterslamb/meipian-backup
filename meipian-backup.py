#!/usr/bin/env python3
"""美篇备份工具 - 把美篇个人主页的全部公开+不公开文章备份到本地。

用法: python meipian-backup.py <美篇号> [选项]
  美篇号: 个人主页 URL meipian.cn/c/<美篇号> 里的那串数字
"""

import argparse
import html as html_lib
import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
from markdownify import markdownify

try:
    from PIL import Image
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_OK = True
except ImportError:
    HEIC_OK = False

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

LIST_API = "https://www.meipian.cn/static/action/load_columns_article.php"
TEMPLATE_API = "https://www.meipian.cn/service/article/template-info?mask_id={mask_id}"
ARTICLE_URL = "https://www.meipian.cn/{mask_id}"
HOME_URL = "https://www.meipian.cn/c/{user_id}"

# Windows/macOS 文件名非法字符
ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def log(msg):
    print(msg, flush=True)


def make_session(user_id):
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Referer": HOME_URL.format(user_id=user_id),
    })
    s.user_id = user_id
    return s


class AcwChallengeError(Exception):
    """ACW 反爬解 cookie 后仍被挡——不应被外层无脑重试。"""


def is_acw_challenge(text):
    """检测响应是否为阿里 ACW 反爬挑战页（不是真正的内容）。"""
    if not text:
        return False
    # 真挑战页有这两个独有标记之一，且总不会有 ARTICLE_DETAIL/usermessage 等正常内容标记
    has_marker = ("aliyun_waf_aa" in text or 'name="aliyunwaf_' in text)
    has_normal = ("ARTICLE_DETAIL" in text or 'class="usermessage"' in text
                  or "load_columns_article" in text)
    return has_marker and not has_normal


def solve_acw_with_playwright(challenge_url):
    """触发挑战时唤起 Playwright 跑一次，从浏览器拿 acw_sc__v2 cookie。"""
    if not PLAYWRIGHT_OK:
        raise RuntimeError("触发了阿里反爬，但 playwright 未安装。请执行 pip install playwright && playwright install chromium")
    log("  ⚠ 触发阿里反爬挑战，启动浏览器自动求解...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=UA)
            page = context.new_page()
            page.goto(challenge_url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(1500)
            cookies = context.cookies()
        finally:
            browser.close()
    if not any(c["name"] == "acw_sc__v2" for c in cookies):
        raise AcwChallengeError("Playwright 未能拿到 acw_sc__v2 cookie")
    sample = next((c["value"] for c in cookies if c["name"] == "acw_sc__v2"), "")
    log(f"  ✓ 拿到 acw_sc__v2={sample[:16]}... 注入 session 继续")
    return cookies


_COOKIE_WHITELIST_NAMES = {"tk", "SESSID", "JSESSIONID", "PHPSESSID"}


def _inject_cookies(session, cookies):
    """把 Playwright cookies 注入 requests session，保留原 domain。
    白名单：acw 前缀（阿里 WAF）+ 常见 session 名（万一服务端双 cookie 校验）。
    """
    for c in cookies:
        name = c["name"]
        if "acw" not in name.lower() and name not in _COOKIE_WHITELIST_NAMES:
            continue
        domain = c.get("domain") or ".meipian.cn"
        session.cookies.set(name, c["value"], domain=domain, path=c.get("path", "/"))


def fetch_with_retry(session, url, *, method="GET", data=None, retries=3, backoff=2.0, stream=False, timeout=30):
    assert retries >= 1
    last_err = None
    for attempt in range(retries):
        try:
            r = session.request(method, url, data=data, stream=stream, timeout=timeout)
            r.raise_for_status()
            # 仅对非流式响应做 ACW 文本检测——访问 r.text 会 drain 整个流，对大文件会内存爆。
            # 流式下载的 ACW 污染由 download_file 的 magic-number 校验兜底。
            if not stream and is_acw_challenge(r.text):
                if method == "GET":
                    challenge_url = url
                else:
                    user_id = getattr(session, "user_id", None)
                    if not user_id:
                        raise RuntimeError("session 未绑定 user_id，无法定位挑战 URL")
                    challenge_url = HOME_URL.format(user_id=user_id)
                cookies = solve_acw_with_playwright(challenge_url)  # 失败抛 AcwChallengeError，下面不重试
                _inject_cookies(session, cookies)
                # 重发当前请求
                r = session.request(method, url, data=data, stream=stream, timeout=timeout)
                r.raise_for_status()
                if not stream and is_acw_challenge(r.text):
                    raise AcwChallengeError("注入 cookie 后仍被反爬挡住")
            return r
        except AcwChallengeError:
            raise  # 不重试：重启 Playwright 没意义
        except KeyboardInterrupt:
            raise
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                wait = backoff * (2 ** attempt)
                log(f"  ! 请求失败 ({e})，{wait:.0f} 秒后重试...")
                time.sleep(wait)
    raise last_err


def parse_home_meta(html):
    """从主页 HTML 抠出昵称、头像、统计数据。返回 dict。"""
    meta = {"nickname": None, "total": None, "avatar_url": None,
            "visit": None, "praise": None, "collect": None}
    m = re.search(r'<div class="info">\s*<h2>([^<]+)</h2>', html)
    if m:
        meta["nickname"] = m.group(1).strip()
    else:
        m = re.search(r'<title>([^<]+?)的专栏\s*-\s*美篇</title>', html)
        if m:
            meta["nickname"] = m.group(1).strip()
    m = re.search(r'文章\s*<p>(\d+)</p>', html)
    if m:
        meta["total"] = int(m.group(1))
    m = re.search(r'class="headerimg"\s*style="background-image:\s*url\([\'"]?([^\'")]+)', html)
    if m:
        meta["avatar_url"] = m.group(1)
    m = re.search(r'<span>被访问</span>\s*&nbsp;\s*(\d+)', html)
    if m:
        meta["visit"] = int(m.group(1))
    m = re.search(r'<span>收获赞</span>\s*&nbsp;\s*(\d+)', html)
    if m:
        meta["praise"] = int(m.group(1))
    m = re.search(r'<span>被收藏</span>\s*&nbsp;\s*(\d+)', html)
    if m:
        meta["collect"] = int(m.group(1))
    return meta


def is_heic_bytes(path):
    """判断文件是否真为 HEIC（按 magic number，不看后缀）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
        # ISO BMFF: bytes 4-7 = "ftyp", bytes 8-11 = brand
        if len(head) < 12 or head[4:8] != b"ftyp":
            return False
        brand = head[8:12]
        return brand in (b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1")
    except OSError:
        return False


def convert_heic_to_jpg(path):
    """HEIC 文件就地转 JPG。返回新路径（.jpg）。失败则保留原文件并返回原路径。"""
    if not HEIC_OK or not is_heic_bytes(path):
        return path
    new_path = path.with_suffix(".jpg")
    try:
        with Image.open(path) as img:
            img.convert("RGB").save(new_path, "JPEG", quality=88, optimize=True)
        path.unlink()
        return new_path
    except Exception as e:
        log(f"    ! HEIC 转 JPG 失败 ({path.name}): {e}")
        return path


def fetch_article_list(session, user_id, list_delay, max_pages=200):
    """翻页拿全部文章 stub 列表。返回 [{id, mask_id, title, create_time, ...}, ...]

    防御：seen_maxid 防服务端 bug 死循环；mask_id 去重；max_pages 兜底；
    dict 错误响应（{"error":"..."}) 主动 raise 而不是静默吞。
    """
    articles = []
    seen_mask = set()
    seen_maxid = set()
    maxid = ""
    for page in range(1, max_pages + 1):
        r = fetch_with_retry(
            session, f"{LIST_API}?userid={user_id}",
            method="POST",
            data={"containerid": "0", "maxid": maxid, "stickmaskid": ""},
        )
        try:
            batch = r.json()
        except json.JSONDecodeError:
            batch = json.loads(r.text)
        if isinstance(batch, dict):
            err = batch.get("error") or batch.get("msg") or batch.get("message") or batch
            raise RuntimeError(f"列表 API 返回错误：{err}")
        if not isinstance(batch, list) or not batch:
            break
        new_items = [a for a in batch if a.get("mask_id") and a["mask_id"] not in seen_mask]
        for a in new_items:
            seen_mask.add(a["mask_id"])
        articles.extend(new_items)
        log(f"  · 第 {page} 页：拿到 {len(batch)} 篇（新增 {len(new_items)}，累计 {len(articles)}）")
        last_id = batch[-1].get("id") or batch[-1].get("article_id")
        if last_id is None:
            log(f"  ! 末篇缺 id 字段，停止翻页")
            break
        next_maxid = str(last_id)
        if next_maxid in seen_maxid:
            log(f"  ! maxid={next_maxid} 已见过，停止翻页（防死循环）")
            break
        seen_maxid.add(next_maxid)
        maxid = next_maxid
        if len(batch) < 10:
            break
        time.sleep(list_delay)
    else:
        log(f"  ! 翻页达到上限 {max_pages}，可能未拉完——请检查")
    return articles


def extract_article_detail(html):
    """从文章页面 HTML 提取 var ARTICLE_DETAIL = {...} JSON。"""
    m = re.search(r'var\s+ARTICLE_DETAIL\s*=\s*(\{)', html)
    if not m:
        raise ValueError("页面里没找到 ARTICLE_DETAIL，美篇可能改版了")
    start = m.start(1)
    # 括号配平
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(html)):
        c = html[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(html[start:i + 1])
    raise ValueError("ARTICLE_DETAIL JSON 括号未配平")


def fetch_template_info(session, mask_id):
    """抓 /service/article/template-info JSON，含模板素材 URL + 主题色。失败返回 None。"""
    try:
        r = fetch_with_retry(session, TEMPLATE_API.format(mask_id=mask_id))
        data = r.json()
        if data.get("code") not in (1000, 200):
            log(f"    ! template-info 异常 code={data.get('code')}")
            return None
        return data.get("data") or None
    except Exception as e:
        log(f"    ! template-info 失败: {e}")
        return None


def _collect_asset_urls(obj, urls):
    """递归收集 template_data 里所有美篇 CDN 装饰素材 URL（排除用户上传内容）。"""
    if isinstance(obj, str):
        # 美篇 CDN 域：ss/ss2/ss-mpvolc/ss-system-mpvolc.meipian.me
        if (obj.startswith("//ss") or obj.startswith("https://ss")) and ".meipian." in obj:
            # /users/{uid}/... 是用户上传内容（已单独下载），其他都是装饰素材
            if "/users/" not in obj:
                urls.add(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_asset_urls(v, urls)
    elif isinstance(obj, list):
        for v in obj:
            _collect_asset_urls(v, urls)


def download_template_assets(session, template_data, template_dir):
    """下载 template-info 里所有装饰素材到 template/ 子目录。返回 {原 URL: 本地相对路径}。
    用 URL hash 前缀防止不同路径的同名文件冲突；HEIC 自动转 JPG。
    """
    import hashlib
    urls = set()
    _collect_asset_urls(template_data, urls)
    if not urls:
        return {}
    template_dir.mkdir(parents=True, exist_ok=True)
    url_map = {}
    for u in sorted(urls):
        full = ("https:" + u) if u.startswith("//") else u
        base = full.rsplit("/", 1)[-1].split("?")[0]
        if not base or "." not in base:
            continue
        # hash 前缀防同名冲突（不同 URL 的同名文件不会互相覆盖）
        slug = hashlib.md5(full.encode("utf-8")).hexdigest()[:8]
        fname = f"{slug}_{base}"
        local = template_dir / fname
        try:
            download_file(session, full, local)
            local = convert_heic_to_jpg(local)  # 模板素材若是 HEIC 转 JPG（浏览器才能渲染）
            url_map[u] = f"template/{local.name}"
        except Exception as e:
            log(f"    ! 模板素材下载失败 {fname}: {e}")
    return url_map


def localize_template_urls(obj, url_map):
    """把 template_data 里所有 URL 字符串替换为本地路径。"""
    if isinstance(obj, str):
        return url_map.get(obj, obj)
    if isinstance(obj, dict):
        return {k: localize_template_urls(v, url_map) for k, v in obj.items()}
    if isinstance(obj, list):
        return [localize_template_urls(v, url_map) for v in obj]
    return obj


def safe_filename(name, max_len=60):
    """清洗成跨平台合法的文件/目录名片段。"""
    name = ILLEGAL_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    name = name.replace(" ", "_")
    if len(name) > max_len:
        name = name[:max_len]
    return name or "untitled"


def build_folder_name(article):
    """日期_标题_mask_id 的目录名。"""
    ts = int(article.get("create_time") or 0)
    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "0000-00-00"
    title = safe_filename(article.get("title", "").strip())
    mask_id = article["mask_id"]
    return f"{date_str}_{title}_{mask_id}"


def guess_ext(url, default=".bin"):
    """从 URL 猜扩展名。美篇 CDN 常见 .heic/.jpg/.png/.mp4/.mp3。"""
    path = urlparse(url).path
    m = re.search(r"\.([A-Za-z0-9]{2,5})$", path)
    if not m:
        return default
    ext = "." + m.group(1).lower()
    # 美篇把 jpg 转成 heic 后缀但实际仍可能是 jpg。保留原后缀。
    return ext


def _looks_like_text_response(head_bytes):
    """二进制下载首字节判断：是否疑似 HTML/JSON 错误页污染。"""
    if not head_bytes:
        return False
    # 常见二进制 magic：识别就放过
    if head_bytes[:3] == b"\xff\xd8\xff":  # jpg
        return False
    if head_bytes[:8].startswith(b"\x89PNG"):  # png
        return False
    if head_bytes[:6] in (b"GIF87a", b"GIF89a"):  # gif
        return False
    if head_bytes[:4] == b"RIFF" and head_bytes[8:12] == b"WEBP":  # webp
        return False
    if len(head_bytes) >= 12 and head_bytes[4:8] == b"ftyp":  # mp4 / heic / m4a / mov
        return False
    if head_bytes[:3] == b"ID3":  # mp3 with id3
        return False
    if head_bytes[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):  # mp3 frame sync
        return False
    # 看起来像文本？
    sample = head_bytes[:512].lower()
    return (b"<html" in sample or b"<!doctype" in sample or b"acw_sc__v2" in sample
            or sample.lstrip().startswith((b"{", b"[")))


def download_file(session, url, dest_path, *, image_delay=0.0):
    """带重试的流式下载。已存在则跳过。下载完用 magic number 校验防止挑战页污染文件。"""
    if dest_path.exists() and dest_path.stat().st_size > 0:
        return dest_path
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_path.with_suffix(dest_path.suffix + ".part")
    head_bytes = b""
    try:
        with fetch_with_retry(session, url, stream=True, timeout=120) as r:
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        if len(head_bytes) < 512:
                            head_bytes += chunk[:512 - len(head_bytes)]
                        f.write(chunk)
        if _looks_like_text_response(head_bytes):
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"下载内容像 HTML/JSON 错误页（前 16 字节: {head_bytes[:16]!r}），疑似被反爬挡住")
        tmp.rename(dest_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    if image_delay:
        time.sleep(image_delay)
    return dest_path


def html_to_md(text_html):
    """段落 HTML 转 Markdown。Quill 输出主要是 <p>/<span style=...>。"""
    if not text_html:
        return ""
    md = markdownify(text_html, heading_style="ATX", strip=["span"])
    # 清掉 markdownify 偶尔留下的空 \xa0 行
    lines = [ln.rstrip() for ln in md.split("\n")]
    # 折叠超过 1 个的连续空行
    out = []
    blank = False
    for ln in lines:
        if not ln.strip():
            if not blank:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    return "\n".join(out).strip()


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-cn">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - {nickname}</title>
<style>
  :root {{
    --c-text: {c_text};
    --c-secondary: {c_secondary};
    --c-accent: {c_accent};
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    font-size: 17px; line-height: 1.75; color: var(--c-text);
    background: #fff;
  }}
  /* 容器：750px 居中，白底 + 装饰图（bgImg, 64px 高的左右边框带）垂直 repeat */
  .article {{
    position: relative;
    width: 750px; max-width: 100%;
    margin: 0 auto;
    background-color: #fff;
    background-image: {bg_top_decl};
    background-position: 50% 0%;
    background-repeat: repeat;
    padding: 0 30px;
    min-height: 100vh;
  }}
  /* 全高主题背景层：fixedBgImg，absolute 在 .article 内，no-repeat，自身下半留白给文字 */
  .article-bgfixed {{
    position: absolute; inset: 0;
    background: {bg_fixed_decl} 50% 0% / 100% auto no-repeat;
    z-index: 1; pointer-events: none;
  }}
  /* 内容浮在最上层，透明背景（主题图留白 + 容器白底自然提供文字背景）
     padding-top 由模板的 caption.top + topDiff 决定，让正文落在主题图的留白区
     左右 30px 让文字内缩（原版 .mp-article-texts 也是这么处理的） */
  .article-content {{
    position: relative; z-index: 3;
    padding: {content_top}px 30px 40px;
  }}
  /* 音乐播放器：右上角 fixed
     收缩态：只 disc(♪)，无胶囊背景
     展开态：只 [▶/⏸ + 音乐名] 胶囊，disc 隐藏（复刻原版） */
  .bgm {{
    position: fixed; top: 15px; right: 30px; z-index: 100;
    display: inline-flex; align-items: center;
    height: 32px;
    font-size: 12px; color: #333;
  }}
  /* 默认收缩：disc 显示，胶囊隐藏 */
  .bgm .disc {{
    width: 32px; height: 32px; border-radius: 50%;
    background: rgba(25,25,25,.6);
    display: flex; align-items: center; justify-content: center;
    color: rgba(255,255,255,.95); font-size: 16px;
    cursor: pointer; user-select: none;
    box-shadow: 0 0 10px rgba(25,25,25,.2);
    flex-shrink: 0;
  }}
  .bgm.playing .disc {{ animation: bgm-spin 4s linear infinite; }}
  @keyframes bgm-spin {{ from {{transform: rotate(0)}} to {{transform: rotate(360deg)}} }}
  .bgm .play-btn, .bgm .name {{ display: none; }}
  /* 展开态：disc 用 visibility hidden 保留宽度（防 hover 丢失），[▶/⏸ + 名] 胶囊在左 */
  .bgm.expanded {{
    padding: 0 0 0 14px;
    background: rgba(255,255,255,.95);
    border-radius: 16px;
    box-shadow: 0 0 10px rgba(25,25,25,.2);
  }}
  .bgm.expanded .disc {{
    visibility: hidden;  /* 不可见但占位，hover 区域稳定 */
    box-shadow: none;
    pointer-events: none;
  }}
  .bgm.expanded .play-btn {{
    display: flex;
    width: 28px; height: 28px; border-radius: 50%;
    background: rgba(25,25,25,.8); color: #fff;
    align-items: center; justify-content: center;
    cursor: pointer; border: none; flex-shrink: 0;
    margin-right: 8px; padding: 0;
    order: -2;  /* 排到 disc 左边 */
  }}
  .bgm.expanded .play-btn:hover {{ background: rgba(25,25,25,1); }}
  .bgm.expanded .name {{
    display: block;
    overflow: hidden; white-space: nowrap;
    max-width: 110px;
    margin-right: 8px;
    order: -1;  /* 排在按钮右侧、disc 左侧 */
    mask-image: linear-gradient(to right, transparent 0, #000 8px, #000 calc(100% - 8px), transparent 100%);
    -webkit-mask-image: linear-gradient(to right, transparent 0, #000 8px, #000 calc(100% - 8px), transparent 100%);
  }}
  .bgm .name span {{ display: inline-block; padding-right: 24px; }}
  .bgm.playing.expanded .name span {{ animation: bgm-marquee 12s linear infinite; }}
  @keyframes bgm-marquee {{ from {{transform: translateX(0)}} to {{transform: translateX(-100%)}} }}
  .bgm audio {{ display: none; }}
  h1 {{ font-size: 26px; line-height: 1.3; margin: 8px 0 6px; color: var(--c-text); }}
  .meta {{ color: var(--c-secondary); font-size: 14px; padding: 10px 0 14px;
           border-bottom: 1px solid rgba(0,0,0,.1); margin-bottom: 22px; }}
  .meta a {{ color: var(--c-accent); word-break: break-all; }}
  .meta .row {{ margin: 3px 0; }}
  img {{ max-width: 100%; height: auto; display: block; margin: 14px auto; border-radius: 2px; }}
  .ql-block {{ margin: 10px 0; }}
  .video-block {{ margin: 18px 0 22px; }}
  .video-block video {{ width: 100%; max-height: 70vh; display: block;
                        background: #000; border-radius: 4px; }}
  .video-controls {{ display: flex; justify-content: space-between; align-items: center;
                     flex-wrap: wrap; gap: 8px; padding: 8px 4px 0; font-size: 13px; color: var(--c-secondary); }}
  .speed-chips {{ display: inline-flex; gap: 4px; }}
  .speed-chips button {{
    border: 1px solid rgba(0,0,0,.2); background: rgba(255,255,255,.85); color: var(--c-text);
    padding: 3px 10px; border-radius: 12px; cursor: pointer; font-size: 12px;
    transition: all .15s;
  }}
  .speed-chips button:hover {{ border-color: var(--c-accent); }}
  .speed-chips button.active {{
    background: var(--c-accent); color: #fff; border-color: var(--c-accent);
  }}
  .footer {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid rgba(0,0,0,.1);
             color: var(--c-secondary); font-size: 12px; text-align: center; }}
  .footer a {{ color: var(--c-accent); }}
  @media (max-width: 760px) {{
    .article {{ padding: 16px; }}
  }}
</style>
</head>
<body>
<div class="article">
  <div class="article-bgfixed"></div>
  <div class="article-content">
{bgm_block}
<h1>{title}</h1>
<div class="meta">
{meta_rows}
</div>
{body}
<div class="footer">由 meipian-backup 备份生成 · 原文 <a href="{origin}">{origin}</a></div>
  </div>
</div>
<script>
// 播放控制 + 视频速度 chips
(function () {{
  // 1. 视频速度切换
  document.querySelectorAll('.video-block').forEach(function (block) {{
    var v = block.querySelector('video');
    if (!v) return;
    block.querySelectorAll('.speed-chips button').forEach(function (btn) {{
      btn.addEventListener('click', function () {{
        v.playbackRate = parseFloat(btn.dataset.rate);
        block.querySelectorAll('.speed-chips button').forEach(function (b) {{
          b.classList.toggle('active', b === btn);
        }});
      }});
    }});
  }});

  // 2. 自定义 bgm 控件：默认收缩；hover/点击展开；离开自动收缩
  var bgmRoot = document.querySelector('.bgm');
  var bgm = bgmRoot ? bgmRoot.querySelector('audio') : null;
  if (bgmRoot && bgm) {{
    var disc = bgmRoot.querySelector('.disc');
    var btn = bgmRoot.querySelector('.play-btn');
    var collapseTimer = null;
    var hovering = false;

    function expand() {{
      bgmRoot.classList.add('expanded');
      if (collapseTimer) {{ clearTimeout(collapseTimer); collapseTimer = null; }}
    }}
    function scheduleCollapse(delay) {{
      if (collapseTimer) clearTimeout(collapseTimer);
      collapseTimer = setTimeout(function () {{
        if (!hovering) bgmRoot.classList.remove('expanded');
      }}, delay);
    }}

    // hover 展开 / 离开收缩。只监听 .bgm，避免 disc display 切换的假 leave
    function onEnter() {{ hovering = true; expand(); }}
    function onLeave() {{ hovering = false; scheduleCollapse(1200); }}
    bgmRoot.addEventListener('mouseenter', onEnter);
    bgmRoot.addEventListener('mouseleave', onLeave);

    // 点击 disc/▶/⏸ 启停（同时给用户视觉反馈：临时展开）
    function toggle(e) {{
      e.stopPropagation();
      expand();
      scheduleCollapse(1800);
      if (bgm.paused) {{
        var p = bgm.play();
        if (p && p.catch) p.catch(function () {{}});
      }} else {{
        bgm.pause();
      }}
    }}
    disc.addEventListener('click', toggle);
    btn.addEventListener('click', toggle);

    // 状态同步——只更新 class/icon，不主动展开（避免视频触发的暂停/恢复也展开）
    bgm.addEventListener('play', function () {{
      bgmRoot.classList.add('playing');
      btn.textContent = '⏸';
    }});
    bgm.addEventListener('pause', function () {{
      bgmRoot.classList.remove('playing');
      btn.textContent = '▶';
    }});
  }}

  // 3. bgm/视频互斥（复刻原版）
  var videos = Array.prototype.slice.call(document.querySelectorAll('video'));
  if (!videos.length && !bgm) return;

  var pausingByVideo = false;
  var userMutedBgm = false;

  if (bgm) {{
    bgm.addEventListener('play', function () {{ userMutedBgm = false; }});
    bgm.addEventListener('pause', function () {{
      if (!pausingByVideo) userMutedBgm = true;
      pausingByVideo = false;
    }});
  }}

  function maybeResumeBgm() {{
    if (!bgm || userMutedBgm) return;
    var anyVideoPlaying = videos.some(function (v) {{ return !v.paused && !v.ended; }});
    if (anyVideoPlaying) return;
    var p = bgm.play();
    if (p && p.catch) p.catch(function () {{}});
  }}

  videos.forEach(function (v) {{
    v.addEventListener('play', function () {{
      if (bgm && !bgm.paused) {{
        pausingByVideo = true;
        bgm.pause();
      }}
      videos.forEach(function (o) {{ if (o !== v && !o.paused) o.pause(); }});
    }});
    v.addEventListener('pause', maybeResumeBgm);
    v.addEventListener('ended', maybeResumeBgm);
  }});
}})();
</script>
</body>
</html>
"""

# 默认配色（无模板时兜底）
DEFAULT_TEMPLATE = {
    "c_text": "#191919", "c_secondary": "#888", "c_accent": "#2379FF",
    "bg_fixed": "transparent",  # 全高背景层
    "bg_top": "#fafafa",         # 容器自身的重复装饰
    "content_top": 0,            # 正文起始 y 偏移（让主题图顶部装饰区独占空间）
}


def _build_bg_decl(url_or_color):
    """把字符串构造成 CSS background 短手值。URL 用 url(), 否则当 color/gradient。"""
    if not url_or_color:
        return "transparent"
    if url_or_color.startswith(("http", "//", "template/", "./")):
        return f"url('{url_or_color}')"
    return url_or_color  # color / gradient / transparent


def html_escape(s):
    return html_lib.escape(s or "", quote=True)


def _resolve_template_vars(template_data):
    """从 template-info JSON（已本地化）抠出渲染所需变量。失败/缺字段用默认值。

    注意：卡片内文字色固定为深色（不跟模板的 s1，因为模板 s1 是为反差自身背景设计的，
    比如春节模板 s1=亮黄用于反差红背景，套到白卡片就看不清）。
    模板色只用于 accent（链接、按钮高亮）。
    """
    v = dict(DEFAULT_TEMPLATE)
    if not template_data:
        return v
    cfg_raw = template_data.get("config")
    cfg = cfg_raw if isinstance(cfg_raw, dict) else {}
    article_cfg = cfg.get("article") if isinstance(cfg.get("article"), dict) else {}
    colors = cfg.get("colors") if isinstance(cfg.get("colors"), dict) else {}
    caption_cfg = cfg.get("caption") if isinstance(cfg.get("caption"), dict) else {}
    # accent: 优先 s3 (蓝/链接色)，否则 s4 (橙/高亮色)
    if colors.get("s3"):
        v["c_accent"] = colors["s3"]
    elif colors.get("s4"):
        v["c_accent"] = colors["s4"]
    # bg_fixed: 全高背景层（fixedBgImg）
    if article_cfg.get("fixedBgImg"):
        v["bg_fixed"] = article_cfg["fixedBgImg"]
    # bg_top: 容器自身重复装饰（bgImg，垂直 repeat-y）
    if article_cfg.get("bgImg"):
        v["bg_top"] = article_cfg["bgImg"]
    # content_top: 正文起始 y 偏移（caption.top + topDiff）让标题落在主题图下半白色区
    if caption_cfg.get("top"):
        try:
            v["content_top"] = int(caption_cfg["top"]) + int(caption_cfg.get("topDiff") or 0)
        except (TypeError, ValueError):
            pass
    return v


def render_html(article, nickname, body_segments, bgm_path, template_data=None):
    """body_segments: 已经按段落顺序拼好的 HTML 字符串列表。"""
    title = (article.get("title") or "").strip()
    ts = int(article.get("create_time") or 0)
    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "未知"
    edit_date = article.get("edit_date_str", "")
    privacy_map = {"1": "公开", "2": "不公开", "3": "加密", "4": "私密"}
    privacy = privacy_map.get(str(article.get("privacy", "")), "")
    origin = f"https://www.meipian.cn/{article.get('mask_id', '')}"

    rows = [f'<div class="row">作者：{html_escape(nickname or "未知")}（美篇号 {html_escape(str(article.get("user_id", "")))}）</div>',
            f'<div class="row">发布时间：{html_escape(date_str)}{("　·　" + html_escape(edit_date)) if edit_date else ""}</div>',
            f'<div class="row">阅读 {article.get("visit_count", 0)} · 点赞 {article.get("praise_count", 0)} · 评论 {article.get("comment_count", 0)} · {html_escape(privacy)}{("　·　IP " + html_escape(article["ip_province"])) if article.get("ip_province") else ""}</div>']

    bgm_block = ""
    if bgm_path:
        music_label = article.get("music_name") or article.get("music_desc") or "背景音乐"
        bgm_block = (
            f'<div class="bgm">'
            f'<div class="disc">♪</div>'
            f'<button class="play-btn" type="button" aria-label="播放/暂停">▶</button>'
            f'<div class="name"><span>{html_escape(music_label)}</span></div>'
            f'<audio loop preload="none" src="{html_escape(bgm_path)}"></audio>'
            f'</div>'
        )

    tv = _resolve_template_vars(template_data)
    return HTML_TEMPLATE.format(
        title=html_escape(title),
        nickname=html_escape(nickname or "未知"),
        meta_rows="\n".join(rows),
        body="\n".join(body_segments),
        origin=html_escape(origin),
        bgm_block=bgm_block,
        c_text=tv["c_text"], c_secondary=tv["c_secondary"], c_accent=tv["c_accent"],
        bg_fixed_decl=_build_bg_decl(tv["bg_fixed"]),
        bg_top_decl=_build_bg_decl(tv["bg_top"]),
        content_top=tv["content_top"],
    )


def render_video_block(local_vid_name, length, thumb_local_name=None):
    """生成 .video-block HTML：含 video + 速度 chips。"""
    poster = ''
    if thumb_local_name:
        poster = ' poster="videos/' + html_escape(thumb_local_name) + '"'
    chip_btns = []
    for r in ("0.75", "1", "1.25", "1.5", "2"):
        cls = ' class="active"' if r == "1" else ''
        chip_btns.append(f'<button data-rate="{r}"{cls}>{r}×</button>')
    chips = '\n      '.join(chip_btns)
    return (
        f'<div class="video-block">\n'
        f'  <video src="videos/{html_escape(local_vid_name)}"{poster} controls preload="none"></video>\n'
        f'  <div class="video-controls">\n'
        f'    <span class="video-cap">视频（{length:.0f} 秒）</span>\n'
        f'    <span class="speed-chips">\n      {chips}\n    </span>\n'
        f'  </div>\n'
        f'</div>'
    )


def render_frontmatter(article, nickname):
    """文章顶部的元信息块（YAML-like，但用 markdown 列表更友好）。"""
    ts = int(article.get("create_time") or 0)
    date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "未知"
    edit_date = article.get("edit_date_str", "")
    privacy_map = {"1": "公开", "2": "不公开", "3": "加密", "4": "私密"}
    privacy = privacy_map.get(str(article.get("privacy", "")), str(article.get("privacy", "")))
    lines = [
        f"# {article.get('title', '').strip()}",
        "",
        f"- 作者：{nickname or '未知'}（美篇号 {article.get('user_id', '')}）",
        f"- 发布时间：{date_str}",
    ]
    if edit_date:
        lines.append(f"- {edit_date}")
    lines += [
        f"- 可见性：{privacy}",
        f"- 阅读 {article.get('visit_count', 0)} · 点赞 {article.get('praise_count', 0)} · 评论 {article.get('comment_count', 0)}",
        f"- 原文：https://www.meipian.cn/{article.get('mask_id', '')}",
    ]
    if article.get("ip_province"):
        lines.append(f"- IP 归属：{article['ip_province']}")
    if article.get("music_name") or article.get("music_desc"):
        music = article.get("music_name") or article.get("music_desc")
        lines.append(f"- 背景音乐：{music}")
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def download_article(session, article_stub, output_root, args, nickname):
    mask_id = article_stub["mask_id"]
    folder = output_root / build_folder_name(article_stub)
    md_path = folder / "index.md"

    html_path = folder / "index.html"
    done = (md_path.exists() and md_path.stat().st_size > 0
            and html_path.exists() and html_path.stat().st_size > 0)
    if done and not args.overwrite:
        log(f"  ↺ 已存在，跳过：{folder.name}")
        return

    # --overwrite 时清干净旧媒体文件，避免文章被作者编辑后旧图被同名复用
    if args.overwrite and folder.exists():
        for sub in (folder / "images", folder / "videos", folder / "template"):
            shutil.rmtree(sub, ignore_errors=True)
        for old in folder.glob("cover.*"):
            old.unlink(missing_ok=True)
        for old in folder.glob("bgm.*"):
            old.unlink(missing_ok=True)

    folder.mkdir(parents=True, exist_ok=True)

    log(f"  → 抓取 {mask_id} ...")
    r = fetch_with_retry(session, ARTICLE_URL.format(mask_id=mask_id))
    detail = extract_article_detail(r.text)
    article = detail["article"]
    # content 可能为 None（脏数据）— 防 NPE
    content_obj = article.get("content") or {}
    segments = content_obj.get("content") or []
    real_nick = (detail.get("author", {}) or {}).get("nickname") or nickname

    # 抓模板信息 + 下素材（失败不阻塞文章本体；整体 try 防 mkdir/IO 异常冒泡）
    template_local = None
    try:
        template_data = fetch_template_info(session, mask_id)
        if template_data:
            url_map = download_template_assets(session, template_data, folder / "template")
            template_local = localize_template_urls(template_data, url_map)
    except Exception as e:
        log(f"    ! 模板获取失败（用默认样式继续）：{e}")

    md_parts = [render_frontmatter(article, real_nick)]
    html_parts = []
    img_idx = 0
    vid_idx = 0
    images_dir = folder / "images"
    videos_dir = folder / "videos"

    for seg in segments:
        text_html = seg.get("text")
        if text_html:
            md = html_to_md(text_html)
            if md:
                md_parts.append(md)
                md_parts.append("")
            html_parts.append(text_html)

        img_url = seg.get("img_url")
        if img_url:
            img_idx += 1
            ext = guess_ext(img_url, ".jpg")
            local = images_dir / f"{img_idx:03d}{ext}"
            try:
                download_file(session, img_url, local, image_delay=args.image_delay)
                local = convert_heic_to_jpg(local)
                md_parts.append(f"![](images/{local.name})")
                md_parts.append("")
                html_parts.append(f'<img src="images/{html_escape(local.name)}" loading="lazy">')
            except Exception as e:
                log(f"    ! 图片下载失败 {img_url}: {e}")
                md_parts.append(f"![图片下载失败]({img_url})")
                md_parts.append("")
                html_parts.append(f'<p style="color:#c33">[图片下载失败：<a href="{html_escape(img_url)}">原 URL</a>]</p>')

        video_url = seg.get("video_url")
        if video_url:
            vid_idx += 1
            ext = guess_ext(video_url, ".mp4")
            local_vid = videos_dir / f"{vid_idx:03d}{ext}"
            try:
                length = float(seg.get("video_length") or 0)
            except (TypeError, ValueError):
                length = 0.0
            try:
                log(f"    ↓ 视频 {vid_idx} ({length:.0f} 秒)...")
                download_file(session, video_url, local_vid)
                thumb = seg.get("video_thumbnail")
                thumb_md = ""
                thumb_name_for_html = None
                if thumb:
                    thumb_ext = guess_ext(thumb, ".jpg")
                    thumb_local = videos_dir / f"{vid_idx:03d}_thumb{thumb_ext}"
                    try:
                        download_file(session, thumb, thumb_local, image_delay=args.image_delay)
                        thumb_local = convert_heic_to_jpg(thumb_local)
                        thumb_md = f"![视频封面](videos/{thumb_local.name})\n\n"
                        thumb_name_for_html = thumb_local.name
                    except Exception:
                        pass
                md_parts.append(f"{thumb_md}[▶ 视频 {vid_idx}（{length:.0f} 秒）](videos/{local_vid.name})")
                md_parts.append("")
                html_parts.append(render_video_block(local_vid.name, length, thumb_name_for_html))
                if args.video_delay:
                    time.sleep(args.video_delay)
            except Exception as e:
                log(f"    ! 视频下载失败 {video_url}: {e}")
                md_parts.append(f"[视频下载失败]({video_url})")
                md_parts.append("")
                html_parts.append(f'<p style="color:#c33">[视频下载失败：<a href="{html_escape(video_url)}">原 URL</a>]</p>')

    # 封面（单独一份，方便预览整个目录）
    cover_url = article.get("cover_img_url")
    if cover_url:
        try:
            ext = guess_ext(cover_url, ".jpg")
            cover_path = folder / f"cover{ext}"
            download_file(session, cover_url, cover_path)
            convert_heic_to_jpg(cover_path)
        except Exception as e:
            log(f"    ! 封面下载失败：{e}")

    # 背景音乐
    bgm_rel = None
    music_url = article.get("music_url")
    if music_url:
        try:
            ext = guess_ext(music_url, ".mp3")
            bgm_local = folder / f"bgm{ext}"
            download_file(session, music_url, bgm_local)
            bgm_rel = bgm_local.name
        except Exception as e:
            log(f"    ! 背景音乐下载失败：{e}")

    md_path.write_text("\n".join(md_parts).rstrip() + "\n", encoding="utf-8")
    html_path.write_text(render_html(article, real_nick, html_parts, bgm_rel, template_local), encoding="utf-8")
    log(f"  ✓ 完成：{folder.name}")


INDEX_HTML = """<!doctype html>
<html lang="zh-cn">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{nickname}的美篇 · 备份索引</title>
<style>
  body {{ margin: 0; font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
          color: #222; background: #f4f5f7; }}
  .header {{ background: linear-gradient(135deg, #2b5876, #4e4376); color: #fff; padding: 36px 24px 28px;
             text-align: center; }}
  .avatar {{ width: 96px; height: 96px; border-radius: 50%; border: 3px solid rgba(255,255,255,.4);
             object-fit: cover; background: #ccc; }}
  .header h1 {{ font-size: 26px; margin: 14px 0 4px; }}
  .header .mid {{ opacity: .8; font-size: 14px; margin-bottom: 16px; }}
  .stats {{ display: inline-flex; gap: 36px; opacity: .95; font-size: 14px; margin-top: 8px; }}
  .stats div {{ text-align: center; }}
  .stats .num {{ font-size: 22px; font-weight: 600; display: block; line-height: 1.2; }}
  .container {{ max-width: 1080px; margin: -16px auto 40px; padding: 0 16px; }}
  .summary {{ background: #fff; padding: 14px 20px; border-radius: 8px; margin-bottom: 20px;
              box-shadow: 0 1px 3px rgba(0,0,0,.06); color: #555; font-size: 14px; }}
  .summary strong {{ color: #2b5876; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 16px; }}
  .card {{ background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.08);
           text-decoration: none; color: inherit; transition: transform .15s, box-shadow .15s; display: flex; flex-direction: column; }}
  .card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 12px rgba(0,0,0,.12); }}
  .card .cover {{ width: 100%; aspect-ratio: 16/9; object-fit: cover; background: #ddd; display: block; }}
  .card .body {{ padding: 12px 14px 14px; flex: 1; display: flex; flex-direction: column; }}
  .card h3 {{ font-size: 16px; line-height: 1.4; margin: 0 0 6px;
              display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
  .card .abs {{ font-size: 13px; color: #777; line-height: 1.5; flex: 1;
                display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
  .card .foot {{ font-size: 12px; color: #999; margin-top: 8px; display: flex; justify-content: space-between; }}
  .card .missing {{ display: inline-block; padding: 1px 6px; border-radius: 3px; background: #fee; color: #c33; font-size: 11px; }}
  .footer {{ text-align: center; color: #aaa; font-size: 12px; padding: 30px 0 20px; }}
</style>
</head>
<body>
<div class="header">
  {avatar_tag}
  <h1>{nickname}的美篇</h1>
  <div class="mid">美篇号 {user_id}</div>
  <div class="stats">
    <div><span class="num">{total}</span>文章</div>
    <div><span class="num">{visit}</span>被访问</div>
    <div><span class="num">{praise}</span>收获赞</div>
    <div><span class="num">{collect}</span>被收藏</div>
  </div>
</div>
<div class="container">
  <div class="summary">备份生成于 <strong>{backup_date}</strong> · 共 <strong>{actual_count}</strong> 篇文章 · 点击任一卡片查看完整内容</div>
  <div class="grid">
{cards}
  </div>
</div>
<div class="footer">由 meipian-backup 备份生成 · 原主页 <a href="https://www.meipian.cn/c/{user_id}">meipian.cn/c/{user_id}</a></div>
</body>
</html>
"""


def render_index_html(meta, articles, output_root, avatar_local):
    cards = []
    actual = 0
    for art in articles:
        mask_id = art.get("mask_id", "")
        title = (art.get("title") or "").strip()
        abstract = (art.get("abstract") or "").strip()
        ts = int(art.get("create_time") or 0)
        date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
        folder_name = build_folder_name(art)
        folder = output_root / folder_name
        cover_rel = ""
        # cover.jpg 优先（浏览器可渲染），.heic 放最后（浏览器不渲染）
        for cand in ("cover.jpg", "cover.jpeg", "cover.png", "cover.webp", "cover.gif", "cover.heic"):
            if (folder / cand).exists():
                cover_rel = f"{folder_name}/{cand}"
                break
        if not cover_rel:
            # 用第一张图兜底（多扩展）
            imgs_dir = folder / "images"
            if imgs_dir.exists():
                imgs = []
                for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp", "*.gif"):
                    imgs.extend(imgs_dir.glob(ext))
                imgs.sort()
                if imgs:
                    cover_rel = f"{folder_name}/images/{imgs[0].name}"
        link = f"{folder_name}/index.html"
        missing = not (folder / "index.html").exists()
        if not missing:
            actual += 1

        cover_html = (f'<img class="cover" src="{html_escape(cover_rel)}" loading="lazy" alt="">'
                      if cover_rel else '<div class="cover"></div>')
        miss_tag = ' <span class="missing">未下载</span>' if missing else ""
        cards.append(
            f'<a class="card" href="{html_escape(link)}">'
            f'{cover_html}'
            f'<div class="body">'
            f'<h3>{html_escape(title)}{miss_tag}</h3>'
            f'<div class="abs">{html_escape(abstract)}</div>'
            f'<div class="foot"><span>{html_escape(date_str)}</span>'
            f'<span>👁 {art.get("visit_count", 0)} · ♥ {art.get("praise_count", 0)} · 💬 {art.get("comment_count", 0)}</span>'
            f'</div></div></a>'
        )

    avatar_tag = (f'<img class="avatar" src="{html_escape(avatar_local)}" alt="">'
                  if avatar_local else '<div class="avatar"></div>')

    return INDEX_HTML.format(
        nickname=html_escape(meta.get("nickname") or "未知"),
        user_id=html_escape(str(meta.get("user_id", ""))),
        total=len(articles),
        actual_count=actual,
        visit=meta.get("visit") or 0,
        praise=meta.get("praise") or 0,
        collect=meta.get("collect") or 0,
        backup_date=datetime.now().strftime("%Y-%m-%d %H:%M"),
        avatar_tag=avatar_tag,
        cards="\n".join(cards),
    )


def render_index_md(meta, articles, output_root):
    lines = [
        f"# {meta.get('nickname') or '未知'}的美篇",
        "",
        f"- 美篇号：{meta.get('user_id', '')}",
        f"- 文章 {len(articles)} 篇 · 被访问 {meta.get('visit') or 0} · 收获赞 {meta.get('praise') or 0} · 被收藏 {meta.get('collect') or 0}",
        f"- 原主页：https://www.meipian.cn/c/{meta.get('user_id', '')}",
        f"- 备份时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "---",
        "",
        "## 文章列表",
        "",
    ]
    for i, art in enumerate(articles, 1):
        title = (art.get("title") or "").strip()
        ts = int(art.get("create_time") or 0)
        date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "?"
        folder_name = build_folder_name(art)
        link = f"{folder_name}/index.md"
        missing = "" if (output_root / folder_name / "index.md").exists() else "  ⚠未下载"
        lines.append(f"{i}. [{title}]({link}) — {date_str} · 阅读 {art.get('visit_count', 0)} · 赞 {art.get('praise_count', 0)} · 评论 {art.get('comment_count', 0)}{missing}")
    return "\n".join(lines) + "\n"


def write_home_index(session, output_root, meta, articles):
    """下载头像，生成 index.html / index.md。"""
    avatar_local = ""
    avatar_url = meta.get("avatar_url")
    if avatar_url:
        try:
            ext = guess_ext(avatar_url, ".jpg")
            avatar_path = output_root / f"avatar{ext}"
            download_file(session, avatar_url, avatar_path)
            avatar_path = convert_heic_to_jpg(avatar_path)
            avatar_local = avatar_path.name
        except Exception as e:
            log(f"  ! 头像下载失败：{e}")

    (output_root / "index.html").write_text(
        render_index_html(meta, articles, output_root, avatar_local), encoding="utf-8")
    (output_root / "index.md").write_text(
        render_index_md(meta, articles, output_root), encoding="utf-8")
    log(f"  ✓ 主页索引：index.html + index.md")


def parse_args():
    p = argparse.ArgumentParser(
        description="美篇备份工具 - 把个人主页的全部文章备份到本地",
        epilog="示例: python meipian-backup.py 12345678",
    )
    p.add_argument("mid", help="美篇号（个人主页 URL 里的那串数字）")
    p.add_argument("-o", "--output", default="meipian-export", help="输出目录（默认 meipian-export）")
    p.add_argument("--article-delay", type=float, default=1.5, help="每篇文章之间的延迟秒数（默认 1.5）")
    p.add_argument("--image-delay", type=float, default=0.3, help="每张图片之间的延迟秒数（默认 0.3）")
    p.add_argument("--video-delay", type=float, default=1.0, help="每个视频之间的延迟秒数（默认 1.0）")
    p.add_argument("--list-delay", type=float, default=0.5, help="列表分页之间的延迟秒数（默认 0.5）")
    p.add_argument("--overwrite", action="store_true", help="覆盖已下载的文章（默认跳过）")
    p.add_argument("--limit", type=int, default=0, help="只下载前 N 篇（用于测试）")
    return p.parse_args()


def main():
    # Windows cmd（cp936）默认对 emoji 报错，先把 stdout 切到 utf-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    args = parse_args()
    if not re.fullmatch(r"\d+", args.mid):
        sys.exit(f"错误：美篇号应该是一串数字（你输入的是：{args.mid}）")
    if args.limit < 0:
        sys.exit(f"错误：--limit 必须 >= 0（你输入的是：{args.limit}）")

    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)

    session = make_session(args.mid)
    try:
        log(f"美篇备份工具 启动")
        log(f"  美篇号：{args.mid}")
        log(f"  输出目录：{output_root.resolve()}")
        if not HEIC_OK:
            log(f"  ⚠ pillow-heif 未安装，HEIC 图片不会自动转 JPG")
            log(f"    建议执行: pip install pillow-heif")
        if not PLAYWRIGHT_OK:
            log(f"  ⚠ playwright 未安装，触发反爬时无法自动求解")
            log(f"    建议执行: pip install playwright && playwright install chromium")
        log("")

        log("[1/4] 抓取主页...")
        try:
            home_html = fetch_with_retry(session, HOME_URL.format(user_id=args.mid)).text
        except Exception as e:
            sys.exit(f"无法访问主页：{e}")
        meta = parse_home_meta(home_html)
        meta["user_id"] = args.mid
        log(f"  · 用户：{meta['nickname'] or '未知'}，主页声称约 {meta['total'] or '?'} 篇")
        log("")

        log("[2/4] 抓取文章列表...")
        try:
            articles = fetch_article_list(session, args.mid, args.list_delay)
        except Exception as e:
            sys.exit(f"列表 API 调用失败：{e}")
        log(f"  · 共拿到 {len(articles)} 篇文章")
        log("")

        all_articles = articles  # 留全量副本给主页索引用
        if args.limit > 0:
            articles = articles[:args.limit]
            log(f"  · --limit 生效，只下载前 {len(articles)} 篇")
            log("")

        log(f"[3/4] 开始下载...")
        failed = []
        interrupted = False
        for i, art in enumerate(articles, 1):
            log(f"[{i}/{len(articles)}] {art.get('title', '').strip()}")
            try:
                download_article(session, art, output_root, args, meta["nickname"])
            except KeyboardInterrupt:
                log("\n用户中断。已下载的文章不受影响，下次运行会从断点继续。")
                interrupted = True
                break
            except Exception as e:
                log(f"  ✗ 失败：{e}")
                failed.append((art.get("mask_id", "?"), art.get("title", ""), str(e)))
            if i < len(articles):
                time.sleep(args.article_delay)

        log("")
        log("[4/4] 生成主页索引...")
        try:
            # 索引始终用全量 articles（即使本次只下了一部分），避免被 --limit 截断
            write_home_index(session, output_root, meta, all_articles)
        except Exception as e:
            log(f"  ! 主页索引生成失败：{e}")

        fail_path = output_root / "失败列表.txt"
        if failed:
            with open(fail_path, "w", encoding="utf-8") as f:
                for mid, title, err in failed:
                    # 去换行避免文件难解析
                    err_oneline = " ".join(err.split())
                    title_oneline = " ".join(title.split())
                    f.write(f"{mid}\t{title_oneline}\t{err_oneline}\n")
            log("")
            log(f"⚠ {len(failed)} 篇下载失败，详见 {fail_path}")
        else:
            # 仅在全量模式（无 --limit）成功时清理上次的旧失败列表；
            # --limit 模式可能用于调试，保留之前的失败记录
            if args.limit == 0 and not interrupted:
                fail_path.unlink(missing_ok=True)
            log("")
            if interrupted:
                log(f"已中断；已成功的文章 + 主页索引已保存。")
            else:
                log(f"✓ 全部 {len(articles)} 篇下载完成 · 主页：{output_root}/index.html")
        if interrupted:
            sys.exit(130)
    finally:
        session.close()


if __name__ == "__main__":
    main()
