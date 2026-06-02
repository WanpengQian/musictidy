# 整合的第三方项目

按角色分类，每项说明：是什么、为什么用它、license、在我们项目里怎么用。

## 库管理

### beets

- **是什么**：成熟的命令行音乐库管理器，10+ 年项目。SQLite 库 + autotagger（基于 MusicBrainz）+ 灵活的 path template + 一堆插件。
- **为什么用**：完整解决 90% 的「扫文件 → 识别 → 整理」问题，没必要重写。
- **License**: MIT
- **Repo**: <https://github.com/beetbox/beets>
- **我们怎么用**：
  - 作为 Python 库 import（不是 subprocess CLI）
  - 不用它的交互式 import session，自己包一层 batch import
  - 用它的 `library.db` 当主数据源
  - 借用它的 path template 引擎做 auto-organize
- **我们用到的 beets 插件**：
  - `chroma` — 接 chromaprint + AcoustID
  - `fetchart` — 抓封面
  - `lyrics` — 抓歌词（P3 才用）
  - `replaygain` — ReplayGain 扫描（P3 才用）

### mutagen

- **是什么**：Python tag 读写库，支持几乎所有音频格式（包括 APEv2、Vorbis Comment、ID3v2 各版本）。
- **为什么用**：beets 内部也用它。retag 时直接调它写文件。
- **License**: GPLv2+
- **Repo**: <https://github.com/quodlibet/mutagen>

## 元数据 / 识别

### MusicBrainz + musicbrainzngs

- **是什么**：开放音乐元数据库（艺人 / 专辑 / 录音 / release-group）+ 它的 Python 客户端。
- **为什么用**：完整度计算需要 ground truth 的 discography。
- **License**: CC0 (data) / BSD (client)
- **要点**：
  - 公共 API rate limit **1 req/sec**，必须串行
  - User-Agent 必须带联系方式（`.env` 里的 `MB_USER_AGENT`）
  - 数据有授权问题（CC0），可以随便缓存

### AcoustID + pyacoustid

- **是什么**：把 chromaprint 指纹映射到 MusicBrainz recording ID 的服务。
- **为什么用**：tag 错乱 / 缺失的文件，靠音频内容也能识别。
- **License**: MIT (client)
- **要点**：免费 API key（注册一个，填 `.env`）；rate limit 比 MB 宽松

### chromaprint (fpcalc)

- **是什么**：音频指纹库 + CLI 工具 `fpcalc`。
- **为什么用**：给 AcoustID 喂指纹。
- **License**: LGPL 2.1
- **怎么装**：FreeBSD `pkg install chromaprint`；macOS `brew install chromaprint`

### Cover Art Archive

- **是什么**：MusicBrainz 配套的封面图源。
- **为什么用**：找 release-group 封面。
- **License**: 各图原 license（我们不重分发，只 hot-link 或缓存）
- **URL 形式**：`https://coverartarchive.org/release-group/{mbid}/front-500`

## 音频处理

### ffmpeg

- **是什么**：所有格式解码 / 编码的瑞士军刀。
- **为什么用**：
  - APE → AAC/FLAC 实时转码
  - 嵌入封面提取
- **License**: LGPL 2.1+ (我们只调它，不集成它的源码)
- **怎么装**：FreeBSD `pkg install ffmpeg`

## Web 框架

### FastAPI + uvicorn

- **是什么**：现代 Python 异步 Web 框架 + ASGI 服务器。
- **为什么用**：原生 async（好搭 ffmpeg/MB 异步调用）、自带 OpenAPI、Pydantic 校验。
- **License**: MIT

### htmx

- **是什么**：用 HTML 属性触发 AJAX，无需 JS 框架。
- **为什么用**：dashboard 这种「服务器渲染、偶尔刷一块」的页面绝配。免去 React/Vue 构建步骤。
- **License**: BSD-2
- **怎么用**：vendor 一份 `htmx.min.js` 到 `server/app/static/`（单文件 ~50KB）

### Jinja2

- **是什么**：Python 模板引擎。
- **为什么用**：FastAPI 标配。
- **License**: BSD-3

## 部署 / 运维

### nginx

- **是什么**：你的 NAS 上已经在跑的反向代理。
- **为什么用**：用户已有，无需引第二个反代。
- **要点**：`proxy_buffering off`（流式音频必需）

### Tailscale

- **是什么**：基于 WireGuard 的 mesh VPN。
- **为什么用**：不开公网端口也能从 iPhone 访问 NAS。
- **License**: BSD-3 (client)，控制面闭源（用官方免费版即可）

---

## 不引入的东西及理由

| 想引入 / 没引入 | 理由 |
|---|---|
| Redis / Celery | 单用户场景；SQLite 当队列已够 |
| Postgres | beets 默认 SQLite；多用户才需要 PG |
| React / Vue / Svelte | dashboard 是服务器渲染场景；htmx 足够 |
| Docker (FreeBSD 上) | FreeBSD 原生跑更舒服；Docker 仅 Mac dev 用 |
| Caddy | 用户 NAS 上已有 nginx |
| Plex / Jellyfin / Subsonic | 那些是「播放器/库」，跟我们「整理工具」职责不同 |
