"""SQLAlchemy engine + ATTACH beets DB.

我们用 SQLite 两个文件：
- our_db (musictidy.db) —— 主连接
- beets_db (library.db) —— 通过 ATTACH 挂到主连接上，schema 别名 "beets"

这样查询里可以直接 JOIN: beets.items ↔ mb_release_group
"""

from collections.abc import Generator
from contextlib import contextmanager
import re
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def _attach_beets_if_needed(cursor, beets_db_path: Path) -> None:
    """ATTACH beets idempotently —— 已挂 / 文件不存在都跳过。"""
    rows = cursor.execute("PRAGMA database_list").fetchall()
    if any(r[1] == "beets" for r in rows):
        return
    if not beets_db_path.exists():
        return
    cursor.execute("ATTACH DATABASE ? AS beets", (str(beets_db_path),))


def _enable_wal_and_attach(beets_db_path: Path) -> "callable":
    """新连接进 pool 时跑：WAL + 首次尝试 ATTACH。"""

    def _setup(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        _attach_beets_if_needed(cursor, beets_db_path)
        cursor.close()

    return _setup


def _attach_on_checkout(beets_db_path: Path) -> "callable":
    """每次从 pool 拿连接时再补一次 ATTACH。

    原因：connect event 只在 connection 第一次建好时跑；如果当时 beets DB 还
    没创建（service 启动 → 用户先 scan），那条 connection 就一直缺少 ATTACH。
    放在 checkout 上，文件出现后立刻补上。idempotent，开销很小。
    """

    def _on_checkout(dbapi_connection, _connection_record, _connection_proxy):
        cursor = dbapi_connection.cursor()
        try:
            _attach_beets_if_needed(cursor, beets_db_path)
        finally:
            cursor.close()

    return _on_checkout


_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        s = get_settings()
        s.ensure_dirs()
        _engine = create_engine(
            f"sqlite:///{s.our_db}",
            connect_args={"check_same_thread": False},
            future=True,
        )
        event.listen(_engine, "connect", _enable_wal_and_attach(s.beets_db))
        event.listen(_engine, "checkout", _attach_on_checkout(s.beets_db))
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    s = get_session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def _is_add_column(stmt: str) -> bool:
    body = "\n".join(ln for ln in stmt.splitlines() if not ln.strip().startswith("--"))
    return bool(re.search(r"\bALTER\s+TABLE\b.*\bADD\s+COLUMN\b", body, re.I | re.S))


def run_migrations() -> None:
    """跑 migrations/*.sql。极简：按文件名顺序 exec。"""
    engine = get_engine()
    migrations_dir = Path(__file__).parent.parent / "migrations"
    if not migrations_dir.exists():
        return
    # 部分迁移 (009/010) 要 ALTER / 建索引 track_fingerprint, 但这表由 ensure_table 建;
    # 全新库先把它建出来, 不然启动直接挂 (fresh install / demo 重建都会踩)
    from app import fingerprint_db  # noqa: PLC0415

    fingerprint_db.ensure_table()

    # 记录已跑过的
    with engine.begin() as conn:
        conn.execute(
            text(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                       filename TEXT PRIMARY KEY,
                       applied_at INTEGER NOT NULL
                   )"""
            )
        )

    for path in sorted(migrations_dir.glob("*.sql")):
        with engine.begin() as conn:
            already = conn.execute(
                text("SELECT 1 FROM schema_migrations WHERE filename=:f"),
                {"f": path.name},
            ).first()
            if already:
                continue
            sql = path.read_text()
            for stmt in sql.split(";\n"):
                if not stmt.strip():
                    continue
                if _is_add_column(stmt):
                    # ADD COLUMN 的表 (如 track_fingerprint) 由 ensure_table() 运行时建/补列:
                    # 全新库时表还不存在, 旧库时列可能已补 —— 两种都跳过, 别让启动挂掉
                    try:
                        with conn.begin_nested():
                            conn.execute(text(stmt))
                    except Exception as e:  # noqa: BLE001
                        msg = str(e).lower()
                        if "no such table" not in msg and "duplicate column" not in msg:
                            raise
                    continue
                conn.execute(text(stmt))
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (filename, applied_at) "
                    "VALUES (:f, strftime('%s','now'))"
                ),
                {"f": path.name},
            )
