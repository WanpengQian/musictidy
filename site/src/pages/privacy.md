---
layout: ../layouts/BaseLayout.astro
title: 隐私政策
description: MusicTidy 的隐私态度：自托管 = 数据不离开你的服务器。
---

<article class="prose">

# 隐私政策

<p class="lead">最后更新：2026 年 6 月</p>

MusicTidy 是一个**自托管**音乐管理工具。它的隐私态度由架构决定：你的音乐、播放历史、元数据、登录凭据，存在你自己运行的服务器上，**不进入任何第三方系统**（除非你显式打开下面列出的少数集成）。

## 我们不收集什么

iOS 客户端**不向 MusicTidy 项目组发送任何数据**。没有遥测、没有崩溃报告（除非你主动通过 iOS 系统设置发送给 Apple），没有用户分析。我们不知道你装了 App，更不知道你听了什么。

服务端代码运行在你自己的机器上，我们对它的运行状态毫无可见性。

## App 本地存的东西

iOS 客户端在你的 iPhone 上保留以下信息，仅用于功能本身：

- **服务器地址**：你在初次启动时填的 host/scheme/port。
- **登录 token**：与你服务器建立会话用的 bearer token。开启了 Face ID / Touch ID 登录后存进 Keychain（受生物识别保护）；否则在 UserDefaults。
- **离线缓存**：你播放或主动下载的音频文件，存在 App 的 Caches 目录。受 5 GB LRU 上限管理；可在"设置 → 离线缓存"页清空或按专辑删除。
- **下载状态、播放队列、UI 偏好**：本地状态，纯客户端用。

这些数据**不会离开手机**。你卸载 App 时，iOS 会把它们全部清除。

## 服务端存的东西

服务端是你拥有 + 运行的，所以"服务端存什么"由你决定。默认安装会写入：

- **beets 数据库**（`library.db`）：你音乐库的元数据索引。
- **MusicTidy 自有数据库**（`musictidy.db`）：MusicBrainz 缓存、任务队列、指纹库、会话表等。
- **专辑封面缓存**：从 Cover Art Archive 拉下来的图片。
- **转码缓存**：临时的 AAC / FLAC 转码结果。

这些都在 `DATA_DIR` 下，跟你的音乐文件一起由你管理。我们看不到。

## 三方调用（默认开启）

服务端为了元数据丰富，会在你触发 scan / identify 时调用：

- **MusicBrainz** (musicbrainz.org) —— 拿艺人 / 专辑 / 曲目元数据。请求里只有 MBID、关键词、指纹查询。**不发送**任何身份信息。
- **AcoustID** (acoustid.org) —— 上传歌曲的 chromaprint 指纹，拿回匹配的 MusicBrainz mbid。请求里只有指纹 + duration。**不发送**文件本身、文件名、用户身份。
- **Cover Art Archive** (coverartarchive.org) —— 按 release-group MBID 拉封面图。

这些都是公开服务，遵循各自的隐私政策。MusicBrainz 在 [musicbrainz.org/privacy](https://musicbrainz.org/privacy) 说明他们记 IP + user-agent；MusicTidy 的 user-agent 是 `MusicTidy/<version>`，不附加你的身份。

如果你不愿意走任何外网，把 `ACOUSTID_API_KEY` 留空即可关闭指纹识别；MusicBrainz 调用同样可以通过断网完全切断（功能上会缺少自动元数据补全）。

## 关于 demo 服务器

我们维护一台 demo 服务器（`demo.musictidy.com`）供试用。这台机器：

- **只跑公共领域 / CC 授权的音乐**，不存在版权问题。
- **每天 04:00 JST 自动重置**，所有人改的东西都会被清掉，不留持久数据。
- **不记录 access log**（cloudflared / nginx 默认日志已禁用）。
- 不要把它当真实库用 —— 你登 demo 之后看到的任何"自己的"数据都会在 24 小时内消失。

## 第三方在 demo 路径上经过的服务

访问 `demo.musictidy.com` 的请求会经过 **Cloudflare**（提供 DNS + 反代 + TLS）。Cloudflare 看得到 IP + 请求 URL；详细政策见 [cloudflare.com/privacypolicy](https://www.cloudflare.com/privacypolicy/)。你自托管的实例如果不走 Cloudflare，则没有这一层。

## 数据出境

App 跟服务端之间走的是你**自己的 TCP 连接**。我们既不能拦截也不能转发。如果你的服务器在中国大陆而你在海外（或反之），数据出境的合规责任在你自己。

## 联系

- 隐私问题 / 数据请求：在 [GitHub Issues](https://github.com/WanpengQian/musictidy/issues) 提一个，加上 `privacy` label。
- 如果是机密内容，可邮件到 `support@musictidy.com`（我们尽快回，但不承诺 SLA）。

</article>
