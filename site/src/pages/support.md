---
layout: ../layouts/BaseLayout.astro
title: Support
description: 用 MusicTidy 遇到问题、想提建议？走 GitHub Issues 最快。
---

<article class="prose">

# Support

<p class="lead">用 MusicTidy 遇到问题、想提建议、想报 bug？以下是最快路径。</p>

## 1) GitHub Issues — 首选

绝大多数问题在这里处理。公开、可追踪、其他用户也能搜得到。

[**github.com/WanpengQian/musictidy/issues**](https://github.com/WanpengQian/musictidy/issues)

提 issue 时建议带上：

- **问题类型**：`bug` / `feature request` / `question` / `docs`
- **复现步骤**（如果是 bug）
- **环境**：iOS 版本、服务端跑在哪种机器 / OS、服务端 commit hash（`git rev-parse --short HEAD`）
- **相关日志**：`sudo journalctl -u musictidy --since '10min ago' | tail -100` 或 `make deploy-logs`

> ⚠ 不要在公开 issue 里贴你服务器的 `APP_PASSWORD`、`ACOUSTID_API_KEY` 或带 token 的请求头。

## 2) 邮件 — 涉及隐私的问题走这里

如果你的问题包含敏感信息（私人音乐库内容、个人服务器 IP / 域名、可能涉及账号），不适合公开：

`support@musictidy.com`

预期回复时间：**3-5 个工作日**。我们是小项目，没专职客服。

## 常见问题速查

### iOS 客户端连不上

1. 进 App 设置 → 服务器 → **测试连接**。如果显示 "MusicTidy v0.3 OK" 绿勾说明握手没问题。
2. 如果失败，常见原因：
   - 协议选错（HTTP 服务器配了 HTTPS）
   - 端口错（默认 8765，HTTPS 默认 443）
   - 防火墙 / NAT 没打通：试 [demo.musictidy.com](https://demo.musictidy.com) 排除客户端本身问题

### 扫库扫不到我的 RAR 包

在浏览器打开 `http://你的服务器:8765/archives`（demo 是 `https://demo.musictidy.com/archives`），看 verdict：

- `ALLOW_FILE_WRITES=false → worker 跳过`：改 `.env`，重启服务
- `unar 没装`：`apt install unar` / `brew install unar`
- `前 16 字节全是 0x00`：你的 RAR 包本身坏了 / 被反吸血站点改写过，换源下

### "目录名怎么没改"（identify 后）

需要两步：

1. POST `/api/v1/admin/backfill-album-artist?dry_run=false` —— 把 canonical artist 名写回 albumartist 字段
2. 进浏览器 `/organize`，逐组点"应用此组" —— 真正 mv 文件

### Pro 解锁后离线缓存还是关着

- 检查"设置 → 离线缓存 → 启用离线缓存"开关
- "仅 WiFi 下载"开着的情况下，蜂窝时被动缓存不工作

### 蜂窝下播放声音卡

蜂窝默认走 server 转码 AAC 192k。如果服务器 CPU 不够（比如树莓派）转码会跟不上：

- 服务端 `.env` 加 `TRANSCODE_WORKERS=2`（默认 5，降低并发减负担）
- 或者干脆用 OfflineCache + 主动下载，提前在 WiFi 下下好

## 报安全漏洞

如果你发现可能影响其他用户的安全问题（比如绕过认证、注入），**不要走 GitHub Issues**。直接发邮件到：

`security@musictidy.com`

会按 [responsible disclosure](https://en.wikipedia.org/wiki/Coordinated_vulnerability_disclosure) 处理，给你 90 天窗口期 + 致谢。

## 项目状态 / 路线图

主路线图：[github.com/WanpengQian/musictidy/issues?q=is%3Aissue+is%3Aopen+label%3Aroadmap](https://github.com/WanpengQian/musictidy/issues?q=is%3Aissue+is%3Aopen+label%3Aroadmap)

近期发布说明：[github.com/WanpengQian/musictidy/releases](https://github.com/WanpengQian/musictidy/releases)

</article>
