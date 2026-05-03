# 美篇备份

长辈在美篇写了几年文章，几百篇，记录半生。
但这些内容只存在美篇服务器上——你想离线看一份、想印本相册、想自己硬盘里留份归档，都办不到：官方 PDF 导出 VIP 限额，第三方工具不靠谱。

这是为这个担心做的小工具：输入一个美篇号，几十分钟把全部文章 + 图片 + 视频 + 背景音乐一次性打包到电脑上，离线永久保存。HTML 完整还原原版页面样式，双击就能看；同时也输出 Markdown，方便用 pandoc 转 Word/PDF。

---

## 它能做什么

- 输入一个美篇号 → 自动抓主页 → 翻页拿到全部文章列表
- 逐篇下载完整内容：标题、正文、图片、视频、视频缩略图、背景音乐、封面、IP 归属、阅读/点赞/评论计数
- **图片自动从 HEIC 转成 JPG**（美篇 CDN 默认给 HEIC 格式，Windows 和大部分浏览器看不了）
- **复刻原版主题模板**（红色记忆、樱花、春节等 50+ 套，每套不同的背景图、配色、装饰边框都下载到本地）
- 生成漂亮的总索引页（封面网格 + 阅读/点赞数据）
- **自动绕过阿里云反爬挑战**（用 Playwright 启浏览器解 cookie 后注入 requests 继续）
- 断点续传：中途挂掉再跑会自动跳过已完成的篇

## 它不做什么（也不打算做）

- 不抓**私密**文章（标记为"仅自己可见"的，必须登录才能看，本工具走匿名路径）
- 不解**加密**文章（需要密码的，v1 不做）
- 不抓评论（v1 不做）
- 不做 GUI、不做 Web 服务、不上架 App Store、不收费、不商业化
- 不要求你提供 cookies 文件——开箱就跑

---

## 给晚辈看的部分（不会编程也能用）

> 家里长辈在美篇写了几年文章，担心哪天 app 出问题或者账号丢失就全没了？这部分教你把它们全部存到自己电脑上。

### 第一步：装 Python

如果你的电脑没装过 Python：

- **Mac / Linux**：打开"终端"应用，输入 `python3 --version`，如果显示版本号（如 `Python 3.11.x`）就有了。没有的话 Mac 装 [官方 Python](https://www.python.org/downloads/macos/)，Linux 用包管理器（如 `sudo apt install python3 python3-pip`）。
- **Windows**：去 [python.org](https://www.python.org/downloads/windows/) 下载，**安装时务必勾选 "Add Python to PATH"**（很重要！）。

### 第二步：下载本工具 + 装依赖

打开终端（Windows 是 PowerShell 或 cmd，Mac/Linux 是"终端"），定位到本工具的目录，然后：

```bash
pip install -r requirements.txt
playwright install chromium
```

第一行装 4 个 Python 库（约 50 MB）。第二行装一个不带界面的浏览器（约 250 MB），用于自动绕过反爬。

### 第三步：找到美篇号

打开长辈的美篇个人主页 URL，类似 `https://www.meipian.cn/c/12345678` —— 末尾那串数字就是美篇号。

也可以让长辈在 app 里点"我"→"个人主页"，里面会显示"美篇号 XXXXXXXX"。

### 第四步：开跑

```bash
python meipian-backup.py <你的美篇号>
```

把 `<你的美篇号>` 换成长辈主页 URL 末尾那串数字。然后等几十分钟（取决于文章数量和视频大小）。

跑完会在当前目录生成一个 `meipian-export/` 文件夹，里面：
- 每篇文章一个独立子目录（按日期 + 标题命名）
- `index.html` 是总目录，**双击就能打开看**
- 每篇里也有 `index.html`，可以播放视频、放背景音乐

> **担心被发现？** 工具默认每篇之间停 1.5 秒、每张图停 0.3 秒，模拟正常浏览节奏。几十篇约半小时跑完，不会引起反爬注意。

### 中途出错了怎么办

- **报错"美篇号格式不对"**：检查输入的是不是数字，没有空格或字母
- **报错"无法访问主页"**：检查网络，或者长辈的账号被设为不公开（这种情况本工具拿不到列表）
- **某篇下载到一半失败**：再跑一次同一条命令即可，会自动跳过已完成的，只补失败的
- **跑了一半被反爬挡住**：等 1 小时让阿里云冷却，或重新跑（工具会自动启浏览器解决）

---

## 给开发者的部分

### 依赖

```
requests>=2.28          # HTTP 客户端
markdownify>=0.11       # HTML → Markdown 转换
pillow-heif>=0.13       # HEIC 解码
playwright>=1.40        # 反爬时启浏览器解 acw_sc__v2 cookie
beautifulsoup4          # markdownify 的依赖（间接）
```

外加 Playwright 需要本地装一份 Chromium（`playwright install chromium`，约 250 MB）。

### 命令行参数

```bash
python meipian-backup.py <美篇号> [选项]
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `mid`（位置参数）| — | 美篇号（必须） |
| `-o, --output` | `meipian-export` | 输出目录 |
| `--article-delay` | `1.5` | 每篇之间延迟秒数（防反爬）|
| `--image-delay` | `0.3` | 每张图之间延迟秒数 |
| `--video-delay` | `1.0` | 每视频之间延迟秒数 |
| `--list-delay` | `0.5` | 列表 API 翻页延迟秒数 |
| `--overwrite` | `False` | 强制重下已存在的文章（默认跳过）|
| `--limit N` | `0` | 只下前 N 篇（用于测试，0 = 全量）|

### 输出目录结构

```
meipian-export/
├── index.html               # 主页索引（所有文章网格卡片）
├── index.md                 # 主页的 markdown 版本
├── avatar.jpg               # 用户头像
├── 2024-03-15_春日游记_a1b2c3d4/
│   ├── index.html           # 文章 HTML（含播放器、模板背景）
│   ├── index.md             # 文章 Markdown
│   ├── cover.jpg            # 文章封面
│   ├── bgm.mp3              # 背景音乐
│   ├── images/              # 正文图片，按出现顺序编号 001.jpg 002.jpg ...
│   ├── videos/              # 正文视频
│   │   ├── 001.mp4
│   │   └── 001_thumb.jpg    # 视频缩略图（用于 video poster）
│   └── template/            # 主题模板素材（背景图、装饰边、遮罩等）
│       ├── <hash>_xxx.png
│       └── ...
└── 失败列表.txt             # 仅失败时存在；成功时自动清理
```

### 关键技术决策

- **直取 `var ARTICLE_DETAIL` JSON**：每个文章页面的 HTML 里内嵌一个完整的结构化 JSON，含全部段落数据。比解析 HTML 选择器稳定得多——后端 model 很少改，前端样式经常改。
- **流式下载 + magic-number 校验**：每个媒体文件下载时检查首字节是否为 jpg/png/mp4/mp3 等已知格式 magic，防止反爬挑战页冒充图片落盘污染。
- **POST 列表 API + cursor 翻页**：`POST /static/action/load_columns_article.php?userid={mid}` form 参数 `containerid=0&maxid={上一篇id}&stickmaskid=`，每页固定 10 条，空数组即末页。加 `seen_maxid` set 和 `max_pages=200` 兜底防死循环。
- **Playwright 仅作为反爬应急**：默认走轻量 requests 路径。检测到响应里有 `aliyun_waf_aa` marker 就唤起 Playwright 跑一次解出 `acw_sc__v2` cookie，注入回 requests session 继续轻量跑。Playwright 不参与正常文章抓取。
- **HEIC → JPG 在下载后立即转**：用 `pillow-heif`，`is_heic_bytes()` 按 magic number 判断（`ftypheic`/`ftypmif1` 等），不看后缀。
- **模板素材本地化**：抓 `/service/article/template-info?mask_id=XXX` JSON 拿到 `fixedBgImg`/`bgImg`/`fixedMask`/`colors`/`caption.top` 等，递归收集所有 CDN URL，下载到 `template/` 子目录（hash 前缀防同名冲突），渲染时把 URL 替换为本地路径。

### 反爬实测

全量跑实测（数十篇含视频的账号）：
- 触发阿里 ACW 反爬约 1-2 次/半小时
- 每次 Playwright 自动求解 + 注入 cookie 后续 requests 继续，约 5 秒延迟
- 全程 0 次人工介入

如果你的 IP 已被烧（短时间内反复测试），单次跑可能触发更多次反爬，每次仍能自动恢复。

### 想魔改的扩展点

- 改 `HTML_TEMPLATE`（约 line 460 起）：调整文章页面样式、播放器外观
- 改 `_resolve_template_vars`：决定模板的哪些字段用本地、哪些用默认
- 改 `safe_filename`：文件名清洗规则
- 加 `--with-comments`：评论抓取（v2+ 计划）
- 加 `--cookie-file`：手动注入 cookie 文件（绕过反爬另一条路）

### 已知限制

- 不抓**私密**文章（仅自己可见的 `privacy=4`，需要登录态）
- 不解**加密**文章（需要密码的 `privacy=3`）
- 阿里 ACW 算法理论上可能升级（如果某天 Playwright 也解不了，需要更激进方案）
- 模板素材占空间：每篇平均 6-7 个装饰 PNG（每个几十 KB）
- 大 PDF 转换不内置：用 `pandoc index.md -o output.pdf` 自己跑

---

## 合规与免责

本工具**仅供个人备份自己（或经授权的）美篇内容使用**。

- 不要用于批量爬取他人账号 → 违反美篇用户协议第 7.1 条
- 不要用于商业用途 → 同上
- 不要修改美篇页面源 → 第 7.2 条
- 不要做 SaaS / 公开服务

---

## 许可

MIT。代码随便改、随便分发，但请保留原始声明，且别拿去做商业服务。

---

## 致谢

- 美篇团队做了一个让普通人也能记录人生的好产品
- 家里的长辈愿意写下这些文章，让本工具有了被需要的理由

如果这个工具帮到了你，欢迎在 GitHub 给个 star。但更重要的：**记得定期跑一次，把长辈的内容多备份一份**。
