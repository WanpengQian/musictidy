# Screenshots

Put iOS app screenshots here. They get referenced by `/` (landing page) and `/deploy`.

## Suggested set

Recommended five screens to capture (matches App Store listing dimensions: **1290 × 2796** for iPhone 6.7"):

1. `01-library.png` — 曲库主页（艺人 row + 专辑 grid）
2. `02-album.png` — 专辑详情（封面 + 曲目列表）
3. `03-player.png` — 全屏播放界面
4. `04-search.png` — 搜索（递进字符 chips）
5. `05-settings.png` — 设置（演示 Pro 解锁 + 缓存详情）

Naming `NN-tag.png` keeps them ordered. The landing page picks the first five it finds matching `[0-9][0-9]-*.png`.

## How to take iOS App Store screenshots quickly

```bash
# iPhone 15 Pro Max simulator → fits 1290×2796
xcrun simctl io booted screenshot --type=png "01-library.png"
```

Or use Xcode → Devices and Simulators → Camera button.
