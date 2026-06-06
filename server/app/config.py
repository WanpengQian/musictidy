"""Pydantic Settings —— 从 .env / 环境变量读所有运行时配置."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),  # 项目根或 server/ 都能找到
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── 必填 ──────────────────────────────────────
    music_root: Path = Field(..., description="主音乐库根目录（向后兼容必填）")
    # 多 root 支持: 额外的库根目录, 逗号分隔 (env 是字符串, 拆 csv).
    # env: MUSIC_ROOTS=/Volumes/NVMe/Music,/Volumes/SSD/Music
    # 不设 → all_roots = [music_root]
    # 设了 → all_roots = [music_root, ...额外的] (去重保序)
    music_roots: str = Field("", description="额外 root, 逗号分隔")

    @property
    def all_roots(self) -> list[Path]:
        """实际扫描 / 路径解析时遍历的所有 root, music_root 永远第一个."""
        extras = [p.strip() for p in self.music_roots.split(",") if p.strip()]
        seen: set[Path] = set()
        out: list[Path] = []
        for r in [self.music_root, *[Path(e) for e in extras]]:
            try:
                rr = r.resolve()
            except Exception:  # noqa: BLE001
                rr = r
            if rr in seen:
                continue
            seen.add(rr)
            out.append(r)
        return out

    def find_root_for(self, path: Path) -> Path | None:
        """绝对 path 落在哪个 root 下, 找到就返回该 root, 没找到 None."""
        try:
            p = path.resolve()
        except Exception:  # noqa: BLE001
            p = path
        for root in self.all_roots:
            try:
                rr = root.resolve()
            except Exception:  # noqa: BLE001
                rr = root
            try:
                p.relative_to(rr)
                return root
            except ValueError:
                continue
        return None

    def to_abs(self, p: Path | str) -> Path:
        """item.path 在 DB 里可能存的是相对 (历史遗留) 或绝对. 给一个绝对 Path:
        - 绝对 → 直接返回
        - 相对 → 依次拼到每个 root 试 exists(), 命中就返回; 都不在就拼 music_root
        """
        pp = Path(p) if not isinstance(p, Path) else p
        if pp.is_absolute():
            return pp
        for root in self.all_roots:
            cand = root / pp
            if cand.exists():
                return cand
        return self.music_root / pp
    bind_port: int = Field(8000, description="HTTP 监听端口")

    # ── 路径 ──────────────────────────────────────
    data_dir: Path = Field(Path("/var/db/musictidy"), description="数据目录")
    bind_host: str = Field("127.0.0.1")

    @property
    def beets_db(self) -> Path:
        return self.data_dir / "library.db"

    @property
    def our_db(self) -> Path:
        return self.data_dir / "musictidy.db"

    @property
    def trash_dir(self) -> Path:
        return self.data_dir / "trash"

    @property
    def transcode_cache_dir(self) -> Path:
        return self.data_dir / "transcode_cache"

    @property
    def covers_dir(self) -> Path:
        return self.data_dir / "covers"

    # ── MusicBrainz ───────────────────────────────
    mb_user_agent: str = Field(
        "MusicTidy/0.1 ( change-me@example.com )",
        description="MB 要求 UA 带联系方式",
    )

    # ── AcoustID（音频指纹识别）───────────────────
    acoustid_api_key: str | None = Field(
        None,
        description="acoustid.org 免费 key；不填则 fingerprint worker 跳过识别",
    )

    # ── 转码 ──────────────────────────────────────
    default_codec_wifi: str = "flac"
    default_codec_cellular: str = "aac"
    default_aac_bitrate: int = 256
    transcode_cache_gb: int = 10
    ffmpeg_concurrency: int = 2

    # ── 任务队列 worker 数 ─────────────────────────
    # 低配机器（树莓派 / 老 NAS）建议 3-5；M 系 Mac mini 可 15-20
    queue_workers: int = 5
    queue_fingerprint_concurrency: int = 3   # AcoustID 3 req/sec 上限
    queue_mb_artist_concurrency: int = 1     # MusicBrainz 1 req/sec 硬限制
    queue_cue_split_concurrency: int = 4     # ffmpeg 吃 CPU + 磁盘 IO,
                                             # 太多并行 API 响应会卡

    # ── 软删 ──────────────────────────────────────
    trash_retention_days: int = 30
    undo_window_sec: int = 5

    # ── 安全闸 ────────────────────────────────────
    allow_file_writes: bool = False

    # ── 自定义品牌（iOS 登录页展示用）─────────────
    # 空 = 显示默认 "MusicTidy" + 客户端内置 logo
    # 设了 = iOS 登录页头部显示你设的名字 / 图，区分多个服务器
    server_display_name: str = Field(
        "MusicTidy",
        description="iOS 登录页显示的服务器名字，默认 'MusicTidy'",
    )
    server_logo_path: Path | None = Field(
        None,
        description="服务器 logo 图绝对路径（PNG/JPG/SVG）；不设则 iOS 用内置 logo",
    )

    # ── 登录 ──────────────────────────────────────
    # 未设置 → 整个 app 跳过 auth（dev 默认）
    # 设置之后所有 /api/v1/* 和 HTML 路由都要 token（除 /login /static /healthz /docs）
    app_password: str | None = Field(None, description="登录密码；空 = auth disabled")
    cookie_secure: bool = Field(False, description="cookie 只走 HTTPS（生产 true）")
    session_ttl_days: int = Field(30)

    def ensure_dirs(self) -> None:
        """启动时确保所有数据目录存在。"""
        for d in (
            self.data_dir,
            self.trash_dir,
            self.transcode_cache_dir,
            self.covers_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
