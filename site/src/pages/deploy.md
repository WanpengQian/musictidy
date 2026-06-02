---
layout: ../layouts/BaseLayout.astro
title: 部署文档
description: MusicTidy 服务端的部署指南：本机、Linux VPS、Cloudflare Tunnel。
---

<article class="prose">

# 部署文档

<p class="lead">MusicTidy 服务端是 Python + FastAPI + SQLite，能跑在任何能装 Python 3.11 的盒子上：旧 Mac mini、群晖 NAS、$5/月的 VPS、树莓派 4 都行。下面分场景给你最短路径。</p>

## 准备

不论怎么部署都需要这些系统包：

```bash
# Debian / Ubuntu
sudo apt install -y python3 python3-venv python3-pip ffmpeg \
                    libchromaprint-tools unar git curl

# macOS (Homebrew)
brew install python@3.11 ffmpeg chromaprint unar
```

什么作用：

- **python3.11+** — 后端运行时
- **ffmpeg** — 蜂窝 / 兼容性转码（FLAC ↔ AAC / MP3）
- **libchromaprint-tools** (`fpcalc`) — 算 AcoustID 指纹
- **unar** — 解 RAR / ZIP / 7z 老资源包（认 GBK / Shift-JIS，比 unrar 友好）

申请一个 **AcoustID API key**：[acoustid.org/api-key](https://acoustid.org/api-key)（免费，注册一下就有）。没这个 key 也能跑，但 fingerprint 自动识别会被关掉。

## 场景 A：本机 / NAS / Mac mini（最简单）

适合自家局域网用，端口直接暴露：

```bash
git clone https://github.com/WanpengQian/musictidy.git
cd musictidy/server
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .

cat > .env <<EOF
APP_PASSWORD=你自己起一个长密码
MUSIC_ROOT=/绝对路径/到/你的/音乐目录
ACOUSTID_API_KEY=你的key
ALLOW_FILE_WRITES=false
BIND_HOST=0.0.0.0
BIND_PORT=8765
EOF

.venv/bin/python -m app.main
```

打开 `http://你的机器IP:8765/healthz` 应该看到 `{"ok":true,"app":"MusicTidy",...}`。

### 让它后台跑

**Linux + systemd**：

```ini
# /etc/systemd/system/musictidy.service
[Unit]
Description=MusicTidy
After=network.target

[Service]
Type=simple
User=你的用户名
WorkingDirectory=/home/你的用户名/MusicTidy/server
EnvironmentFile=/home/你的用户名/MusicTidy/server/.env
ExecStart=/home/你的用户名/MusicTidy/server/.venv/bin/python -m app.main
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now musictidy
sudo journalctl -u musictidy -f
```

**macOS + launchd**：写一份 `~/Library/LaunchAgents/com.musictidy.server.plist`，参考 Apple 文档；或者直接用 `nohup .venv/bin/python -m app.main &` 跑也行。

## 场景 B：公网 VPS + 反代

适合在家以外能访问：iPhone 出差用、跟朋友共享。

最简洁的拓扑：

```
iPhone ──HTTPS──> Cloudflare 边缘 ──Tunnel──> VPS 上的 cloudflared ──HTTP──> localhost:8765
```

好处：

- **零端口暴露**：VPS 上不开任何对外端口
- **自动 HTTPS**：CF 边缘给你做 TLS，cert 自动续
- **抗 DDoS**：CF 边缘吸收
- 不跟 VPS 上已有的 nginx / xray 等抢 :443

### 步骤

1. 域名解析交给 Cloudflare（Registrar 转过去最省事）
2. VPS 上按"场景 A"装好 MusicTidy，绑 `127.0.0.1:8765`（**不要** `0.0.0.0`）
3. Cloudflare Dashboard → Zero Trust → Networks → Tunnels → Create tunnel
4. 选 `cloudflared` 连接器，复制安装命令（含 token）到 VPS 上跑
5. Tunnel 详情 → **Routes** 加 public hostname：
   - Subdomain: `musictidy`（或你想用的）
   - Domain: 你的域名
   - Service: **HTTP** `localhost:8765`
6. 几秒后 `https://musictidy.你的域名` 就直接可访问

iOS 客户端用 `https://musictidy.你的域名`、端口留空（默认 443）即可。

## 关键 .env 字段

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `APP_PASSWORD` | (必填) | 登录密码。**至少 12 位、别复用** |
| `MUSIC_ROOT` | (必填) | 你音乐根目录的绝对路径 |
| `DATA_DIR` | `./data` | beets DB + 我们自己 DB + 缓存目录 |
| `TRASH_DIR` | `./trash` | 解压完源档 / 被替换的旧文件 |
| `ACOUSTID_API_KEY` | (空) | acoustid.org 拿。空 = 关 fingerprint |
| `ALLOW_FILE_WRITES` | `false` | **改成 `true`** 才能 organize / 解压 / cue-split |
| `BIND_HOST` | `127.0.0.1` | 公网 VPS 用 `0.0.0.0` 或经 tunnel |
| `BIND_PORT` | `8765` | |
| `COOKIE_SECURE` | `true` | HTTP 部署改 `false`，HTTPS 部署保持 `true` |
| `TRANSCODE_WORKERS` | `5` | 转码并发；低配机降到 2 |

## 灌音乐 / 首次扫库

把所有音乐文件复制 / `rsync` 进 `MUSIC_ROOT`，目录结构随便。然后：

```bash
TOKEN=$(curl -s -X POST http://localhost:8765/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"你的密码"}' | jq -r .token)

# 触发扫库
curl -s -X POST http://localhost:8765/api/v1/admin/scan \
  -H "Authorization: Bearer $TOKEN"

# 看进度
watch -n 3 'curl -s "http://localhost:8765/api/v1/admin/stats" -H "Authorization: Bearer '$TOKEN'" | jq .'
```

扫库流程：

1. **scan** —— 把音频文件入库（beets）。lossless / 高码率优先
2. **fingerprint** —— 对每首歌算 chromaprint → 查 AcoustID → 写回 mb_* 字段
3. **mb_fetch_release_group / artist** —— 拿到完整的 release-group / 艺人元数据 + 封面
4. **archive_extract** —— 自动解 RAR / ZIP / 7z（需 `ALLOW_FILE_WRITES=true`）
5. **cue_split** —— CUE+APE / CUE+FLAC 自动分轨（同上）

中途遇到问题，看：

- `GET /healthz` —— 服务活着否
- `GET /admin/stats` —— 总进度
- `GET /admin/queue` —— 队列状态分桶
- `GET /admin/queue/recent` —— 最近 20 个任务（含 `last_error`）
- `GET /archives` —— 压缩包诊断页（HTML）

## 备份

需要持久保留的：

- **音乐文件**（你的真正资产，跟服务器分两份盘）
- `DATA_DIR/musictidy.db*` —— 我们的 DB，含封面偏好 / 指纹库 / 任务历史
- `DATA_DIR/library.db` —— beets 索引（可以从音乐文件 rescan 重建，但费时）
- `DATA_DIR/covers/` —— 缓存的专辑封面（可重建）
- `.env` —— 配置

简单脚本（每天打一份）：

```bash
#!/bin/bash
DATE=$(date +%F)
BACKUP=/backups/musictidy
mkdir -p "$BACKUP"
tar czf "$BACKUP/data-$DATE.tar.gz" -C /path/to/server data .env
# 老于 30 天的删掉
find "$BACKUP" -name 'data-*.tar.gz' -mtime +30 -delete
```

## 升级 / 拉新版

```bash
cd /path/to/MusicTidy
git pull
cd server
.venv/bin/pip install -e .
sudo systemctl restart musictidy
```

如果有数据库 schema 改动，会自动跑 migrations（`migrations/*.sql` 按文件名排序执行）。升级前**先备份** `DATA_DIR`，特别是大版本之间。

## 排错速查

| 症状 | 大概率原因 |
| --- | --- |
| `/healthz` 拿不到 | service 没起 / 端口被占 / firewall |
| iOS 客户端连接超时 | BIND_HOST 还是 127.0.0.1 但 iOS 不在本机 |
| 扫完 0 items_total | MUSIC_ROOT 路径错 / 没读权限 |
| fingerprint 一直 fail | `ACOUSTID_API_KEY` 没填 / 网络出不去 acoustid.org |
| organize 不动文件 | `ALLOW_FILE_WRITES=false` |
| RAR 解压不了 | 系统没装 `unar` / 文件本身坏了（看 `/archives`） |
| 蜂窝播放卡 | 服务器 CPU 跟不上转码，降 `TRANSCODE_WORKERS` |

更多疑难：[Support](/support) 页或 [GitHub Issues](https://github.com/WanpengQian/musictidy/issues)。

</article>
