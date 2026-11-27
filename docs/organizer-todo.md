# 整理 (Organizer) 改造 TODO —— 结构优先

背景: 纯 AcoustID 指纹全网猜不准 (华语/日语冷门碟尤甚)。真实收藏大多已按
「专辑打包」或「按歌手放」, 信息其实就在文件夹里 (目录名 / 文件名 / tag / CUE /
EAC 日志), 只是散落 + 编码乱。改成: 本地解析为主, 外部库只做确认/补全。

## 第 1 层 本地解析 (不联网, 主力)
- [x] `app/folder_parse.py`: 目录 → {歌手, 专辑, 年份, 曲目[碟/轨/标题], 置信度, 证据}
  - [x] 目录名模式: `歌手/专辑/`、`歌手-专辑`、`歌手 - 专辑`、`[歌手] 专辑`、`歌手《专辑》`、`年份 专辑`、只有专辑名; 剥 `[WAV+CUE]` `(FLAC)` `320K` 等噪声
  - [x] 文件名: `NN. 标题` / `N-NN 标题` / `NN 歌手 - 标题`
  - [x] tag (mutagen): artist / album / albumartist / title / track / disc / date
  - [x] CUE: PERFORMER / TITLE / 曲目 (复用 cuesplit 的编码评分)
  - [x] EAC / XLD 日志: 头部 `歌手 / 专辑` + 每轨文件名 → 曲名 (Big5/GBK/UTF-16)
  - [x] 宽松 CUE (无引号 / `TRACK 01-歌名 AUDIO`) + 说明文件 intro.txt/nfo (`专辑名称:勇敢`)
  - [x] 清洗: latin1 误读的 GBK/Big5/SJIS 乱码还原、论坛水印 `[xxx论坛]`、简繁归一 (比较用)
  - [x] 多源投票出结果 + 置信度 (high / medium / low) + 冲突说明
- [x] 单测 `tests/test_folder_parse.py` (覆盖上面每种模式)
- [x] 报表脚本 `tools/parse_report.py`: 扫目录 → 表格 (歌手/专辑/曲目数/置信度/证据), 用真实档案验收

## 第 2 层 歌手别名表
- [x] `artist_alias` 表 (musictidy.db): 归一化别名 → 规范名 (+ artist_mbid 可空)
- [x] 归一化: 大小写/空格/标点/简繁 (`張惠妹`=`张惠妹`, `A-mei`=`Amei`)
- [x] 种子: Library 现有艺人目录 + mb_artist 缓存 + 用户审批时手填 (审批即学习)
- [x] 联网补: MB artist 搜索带 aliases (缓存), 把 `A-mei Cheung` / `Amei` 挂到 张惠妹
- [x] approve 时艺人名走别名表归一 + 学习目录里出现过的各种写法
- [x] Library 已分裂艺人合并: `plan_library_merge` / `apply_library_merge` (dry-run 已验证, **待用户确认执行**)

## 第 3 层 限定范围外部确认 (可选, 联网)
- [x] `app/album_confirm.py`: 已知歌手 → MB browse 该歌手的 release-group (缓存) → 专辑名模糊匹配 (归一化+简繁+所有专辑票)
- [x] 候选 release 曲目表 vs 目录: 时长序列逐轨对位打分 (支持只有部分碟) → 确认后补 MBID/年份/曲名; 本地多来源一致的专辑名不被 MB 覆盖
- [x] 标题对不上 → 各轨时长拼近似 TOC 走 MB 模糊 DiscID (BAD BOY 靠这条 10/10 确认)
- [x] 找不到不影响入库 (本地解析结果照样可批)
- [x] 后台跑 + 进度 (`POST/GET /inbox/confirm`), MB 响应和确认结果都落缓存

## 第 4 层 AcoustID 兜底
- [x] 本地 high 不出指纹候选; medium 时排在本地候选后; low 时指纹候选排前面

## 接入整理流程
- [x] compute_proposals: 每个目录附 `parsed` + 置信度; 本地解析结果作为首位候选; 按歌手放的目录按 tag 专辑拆组
- [x] approve: 接收逐曲 {碟, 轨, 标题} (用解析结果命名), 歌手走别名表
- [x] 高置信目录一键批量批准 (`POST /inbox/approve-high`)
- [x] 前端 Inbox 展示解析结果/投票/证据, 专辑/歌手/年份/曲名可就地改; 联网确认 + 批量批准按钮; Library 合并提示条 (**UI 未在浏览器里实际点过**)
- [x] 端到端测试 `tests/test_inbox_flow.py` (ffmpeg 生成真 flac → 提议 → 批量批准 → Library + beets + 别名)

## 附带发现
- [x] `最红 CD1_tracks` 这类拆轨目录碟号认不出 → 多碟同轨号被 inbox 去重吞掉。已改用 folder_parse 的碟号
- [x] Library: Avicii 错绑 mbid + `动力火车`/`動力火車` 分裂 → 用户已点「合并」
- [x] 全新库启动挂掉: 迁移 009/010 ALTER 的 `track_fingerprint` 由 ensure_table 运行时建 → run_migrations 先建表 + ADD COLUMN 容错 (fresh install / demo 重建都会踩)
- [x] 多碟专辑批准后被后台 mega 护栏解绑 (按 `Disc N` 子目录算覆盖率, 每碟 <60%) → 护栏改按整张专辑算; 《最紅》已修复重绑
- [x] 确认写入的 rg 缓存 primary_type 为空 → 播放器 (只列 Album/EP) 看不到; 改走 ensure_rg_in_cache 拿 MB 真实类型
- [x] demo 自 6/7 起 502 (上面那个全新库启动 bug) → 已 release + deploy (public b68f75d)
- [ ] 老测试 10 个过时失败 (管理接口现在要登录 401 / Jinja 首页已下线 404 等), 原先被迁移报错掩盖

## 原生 macOS 整理 App (2026-09-25 起, 取代网页整理页为主力)
流程: 用户自己在 Finder 把原始文件拷进整理文件夹 → App 监听自动出现 → 审阅/批准 → 核实可安全删除 → 用户确认移到回收站 (30 天内可恢复)。
- [x] 服务端「可安全删除」核实 `app/inbox_cleanup.py` + `GET/POST /inbox/cleanup`, 回收站 `GET /inbox/trash` / `POST /inbox/trash/restore` (有着落 = 已入库 / 被更好格式顶替 / 整轨已拆且分轨入库; 压缩包须已解压; 拷贝中不给删; 空目录不进一键清理)
- [x] `GET /inbox/config` (整理文件夹位置) + `GET /inbox/preview` (APE 等系统播不了的格式 ffmpeg 转流, 只许读整理文件夹)
- [x] `mac/` 独立 XcodeGen 工程 (不动 iOS 工程): 登录 (token 存 Keychain) / FSEvents 监听整理文件夹 (3 秒去抖) / 左栏 待确认·高置信·已入库 / 详情 解析卡片+候选+可改专辑歌手年份曲名+试听 / ⌘↩ 批准并跳下一张 / 批准全部高置信 / 联网确认进度 / 清理面板+回收站恢复
- [ ] 多语言 (现在界面只有中文)
- [ ] 正式签名 / 打包分发 (现在是 ad-hoc 签名的 Debug 构建; 每次重编签名变, 读 Keychain 可能会弹一次授权)
- [ ] 拷贝中目录在左栏的状态显示 (目前只在清理面板里有「拷贝中」)
- [ ] 网页整理页降级为兜底 (服务器在 NAS/Linux 的用户用), 不再加新功能

## 重复整理 + 年份来源 (2026-09-25)
- [x] 同一张专辑换了个文件 (另一版 / mp3 / 改 tag) 按专辑+曲目跟 Library 比 (`app/library_match.py`): have 不拷可清理 / new 补进同一张 / upgrade 用户选替换 (旧版进 trash/library) 或保留库里的 (记 inbox_dismissed, 可清理); 批量批准不碰要替换的
- [x] 年份候选按来源列出, App 里点选: tag / 说明文件 / 目录名 / MusicBrainz 首发 / MusicBrainz 该版本 (再版) / Wikidata (`app/album_dates.py`)
  - Wikidata 条目: 先走 MB release group 的 wikidata 链接; 没有再用 SPARQL 列该歌手 (MB 歌手链接的 QID) 全部专辑按名字对 (去「(張惠妹專輯)」消歧义后缀)
  - 默认: MB 首发 > Wikidata > 说明文件 > tag > 目录名 > MB 该版本; 写入 beets year/month/day
- [x] 修: 联网确认原来用匹配到的 release 日期 (可能是再版) → 改用 release group 首发
- [x] 修: App 轮询刷新时 compute_proposals 清空再填 _last_folders, 并发读到半截 → 建好再整体替换
- [ ] Library 里已入库但没年份的专辑 (10/17 张) 补年份: 同一套来源 + 用户点选
