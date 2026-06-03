# 用 Cloudflare Tunnel 把你的 MusicTidy server 接通公网

这是**推荐**的部署方式。不需要：
- 路由器开端口转发
- 静态公网 IP（家宽常没有）
- 自己配 HTTPS / 证书续签

不需要：
- 信用卡（CF Tunnel 个人用永久免费）

只需要：
- 一个域名挂在 Cloudflare 上（10 美元 / 年起，CF Registrar 平价）

总耗时：**~5 分钟**

---

## 1. 注册 Cloudflare 账号 + 加域名

- [https://dash.cloudflare.com/sign-up](https://dash.cloudflare.com/sign-up) 注册
- 点 "Add Site" 把你想用的域名加进来
  - 如果你还没域名，可以直接在 CF Registrar 买（不会被坑域名续费）
- 把域名 nameservers 改成 CF 给的那两个，等 10 分钟 propagation

## 2. 装 cloudflared

```bash
# macOS
brew install cloudflared

# Debian / Ubuntu
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -o /tmp/cf.deb
sudo dpkg -i /tmp/cf.deb

# RHEL / Fedora
sudo rpm -ivh https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-x86_64.rpm
```

## 3. 让 cloudflared 拿到 CF 账号授权

```bash
cloudflared tunnel login
```

会打开浏览器，选你刚加的那个域名。完成后 `~/.cloudflared/cert.pem` 会出现。

## 4. 创建 tunnel

```bash
cloudflared tunnel create musictidy
```

输出里有一串 UUID 和一个 `.json` 凭证文件路径，记下来。

## 5. 让 `m.<your-domain>.com` 指向这条 tunnel

```bash
cloudflared tunnel route dns musictidy m.<your-domain>.com
```

> 子域名你随便挑，用 `m`、`music`、`tidy` 等都行。下文用 `m.example.com` 占位。

## 6. 写 cloudflared 配置

`~/.cloudflared/config.yml`:

```yaml
tunnel: <UUID-从-step-4>
credentials-file: /home/<你的用户名>/.cloudflared/<UUID>.json

ingress:
  - hostname: m.example.com
    service: http://localhost:8765
  - service: http_status:404
```

测一下：

```bash
cloudflared tunnel run musictidy
```

另开一个终端：

```bash
curl https://m.example.com/healthz
# {"ok":true,"app":"MusicTidy",...}
```

通了 → ctrl-C 关掉前台版本，往下走持久化。

## 7. 装成系统服务（开机自启）

### macOS (LaunchAgent，无需 sudo)

`~/Library/LaunchAgents/com.musictidy.cloudflared.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.musictidy.cloudflared</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/homebrew/opt/cloudflared/bin/cloudflared</string>
        <string>tunnel</string>
        <string>--no-autoupdate</string>
        <string>--config</string>
        <string>/Users/YOU/.cloudflared/config.yml</string>
        <string>run</string>
        <string>musictidy</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>/Users/YOU/.cloudflared/tunnel.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/.cloudflared/tunnel.log</string>
</dict>
</plist>
```

加载：

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.musictidy.cloudflared.plist
```

### Linux (systemd，需要 sudo)

```bash
sudo cloudflared service install
# 自动写 /etc/systemd/system/cloudflared.service + 自启
sudo systemctl status cloudflared
```

## 8. 用 iOS / Web 客户端连

- **iOS**：app 内填 `m.example.com`（scheme https，port 留空）
- **Web**：打开 [https://app.musictidy.com](https://app.musictidy.com) → 填同样地址

零开放端口、自动 HTTPS、走 CF 边缘所以延迟也低。

---

## 故障排查

### "Failed to add route ... A record already exists"

子域名已经被占用。两个办法：
1. `cloudflared tunnel route dns --overwrite-dns musictidy m.example.com` 强覆
2. 去 CF dashboard DNS 删了那条旧记录，重跑 step 5

### tunnel.log 一直 reconnect

99% 是 `config.yml` 里 `credentials-file` 路径写错。检查那个 `.json` 在不在。

### `curl https://m.example.com/healthz` 502

CF tunnel 通了但 server 没起。查 `lsof -i:8765` 看 MusicTidy server 在不在跑。

### CF 长亮 active 但 iOS 报 SSL 错

iOS 缓存了旧证书。卸了重装 app。
