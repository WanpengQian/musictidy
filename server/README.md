# MusicTidy Server

FastAPI + beets + SQLite + ffmpeg。跨平台 Python，部署目标 FreeBSD。

## 本地起步

```sh
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
cp ../.env.example ../.env
$EDITOR ../.env          # 至少改 MUSIC_ROOT
python -m app.main
```

打开 `http://127.0.0.1:8000`。

## 目录

```
app/
├── main.py          # FastAPI 入口 + 启动钩
├── config.py        # Pydantic Settings 读 .env
├── db.py            # SQLAlchemy engine; ATTACH beets DB
├── beets_bridge.py  # 调 beets 的薄封装
├── models/          # 表映射
│   ├── beets_view.py
│   └── ours.py
├── api/             # JSON 路由
│   ├── library.py
│   ├── curation.py
│   ├── admin.py
│   └── web.py       # HTML 路由（htmx）
├── workers/         # 后台任务
│   ├── scan.py
│   ├── fingerprint.py
│   ├── musicbrainz.py
│   ├── coverart.py
│   ├── trash_gc.py
│   └── scheduler.py
├── transcode/       # ffmpeg 流式
│   ├── ffmpeg.py
│   └── cache.py
├── templates/       # Jinja
└── static/          # htmx.min.js + app.css
migrations/          # 我们的 SQL schema
tests/
```

## 测试

```sh
pytest
```

## Lint / Format

```sh
ruff check .
ruff format .
```
