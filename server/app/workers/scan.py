"""文件系统扫描 → 投喂 beets import.

流程：
1. walk MUSIC_ROOT，找所有 audio 扩展名
2. 跟 beets 已知 paths 做 diff
3. 新文件逐个 import_file（不动文件，只读 tags 写 DB）
4. 给所有还没指纹化/识别的 item enqueue 'fingerprint'（占位，worker 后续实现）

第一次跑可能要几分钟（取决于 IO + tag 读取），之后增量很快。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from pathlib import Path

from app import archive, beets_bridge, cuesplit
from app.config import get_settings
from app.workers import queue

# 全局 scan 锁：APScheduler / admin endpoint / iOS 下拉同时触发也只跑一个
_scan_lock = asyncio.Lock()

log = logging.getLogger(__name__)

AUDIO_EXTS = {
    ".flac", ".ape", ".mp3", ".m4a", ".aac",
    ".ogg", ".opus", ".wav", ".wv", ".alac",
}


def walk_audio_files(root: Path) -> Iterator[Path]:
    """递归 yield 所有音频文件的绝对 path."""
    if not root.exists():
        log.warning("MUSIC_ROOT 不存在: %s", root)
        return
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in AUDIO_EXTS:
            continue
        # 跳过隐藏目录（.trash, .git, .Spotlight-V100, ...）
        if any(part.startswith(".") for part in p.relative_to(root).parts):
            continue
        yield p.resolve()


def _is_file_stable(path: Path, settle_s: float = 1.5) -> bool:
    """用户可能正在拷贝中, 看 size/mtime 在 settle_s 秒内是否一致.

    一致 = 没在写; 不一致 = 还在写 → 跳过这次 scan, 等下次再来.
    settle_s 故意小 (1.5s), 大多数 copy 一会儿就稳, 不想让 scan 卡太久.
    文件不存在 (用户中途删了) 也算 unstable.
    """
    try:
        s1 = path.stat()
    except OSError:
        return False
    time.sleep(settle_s)
    try:
        s2 = path.stat()
    except OSError:
        return False
    return s1.st_size == s2.st_size and s1.st_mtime == s2.st_mtime


async def scan_and_import() -> dict[str, int]:
    """主入口：扫库 → import 新文件 → enqueue 指纹 + CUE 切轨任务.

    并发安全：全局 _scan_lock 保证同时只跑一个 scan。第二个调用
    立即返回 skipped 而不是等锁——免得 iOS 下拉刷新累加阻塞.

    walk_audio_files / import_file / detect_pairs / stale cleanup 都是
    同步 IO, 数 GB 库扫完要几十秒 ~ 几分钟; 直接 await 会堵死事件循环,
    导致 API stall, 30-min auto-scan 调度被 miss. 整个主体丢 to_thread。
    """
    if _scan_lock.locked():
        log.info("scan: already running, skipping this trigger")
        return {"skipped": True, "reason": "already running"}

    async with _scan_lock:
        return await asyncio.to_thread(_do_scan_blocking)


def _do_scan_blocking() -> dict[str, int]:
    s = get_settings()
    started = time.time()
    roots = s.all_roots
    log.info("scan: starting; roots=%s", [str(r) for r in roots])

    lib = beets_bridge.get_library(s.beets_db, s.music_root)

    known = beets_bridge.all_known_paths(lib)
    log.info("scan: %d already in beets", len(known))

    # 先找 CUE+源音频对 —— 跨所有 root 扫
    cue_pairs: list = []
    for r in roots:
        cue_pairs.extend(cuesplit.detect_pairs(r))
    cue_src_paths = {p[1].resolve() for p in cue_pairs}
    log.info("scan: 发现 %d 个 CUE+音频对", len(cue_pairs))

    stats = beets_bridge.ImportStats()
    new_ids_to_fp: list[int] = []

    def _walk_all_roots():
        for root in roots:
            yield from walk_audio_files(root)

    for path in _walk_all_roots():
        stats.scanned += 1
        if path in known:
            stats.skipped += 1
            continue

        # 用户拷贝进行中? 文件大小 / mtime 在短时间内还在变 → 跳过等下次 scan
        # (避免 import 进半成品 → fingerprint 算的指纹是部分内容 → 识别错)
        if not _is_file_stable(path):
            stats.skipped += 1
            log.info("scan: %s 还在写入 (size/mtime 不稳), 跳过等下次", path.name)
            continue

        item_id = beets_bridge.import_file(lib, path)
        if item_id is None:
            stats.failed += 1
        else:
            stats.added += 1
            # CUE 整轨源 → 不排指纹（让 cue_split worker 处理后产生的分轨再来排）
            if path.resolve() not in cue_src_paths:
                new_ids_to_fp.append(item_id)

        if stats.scanned % 1000 == 0:
            log.info(
                "scan: %d scanned, %d added, %d skipped, %d failed",
                stats.scanned, stats.added, stats.skipped, stats.failed,
            )

    if new_ids_to_fp:
        queue.enqueue_many(
            "fingerprint",
            [{"item_id": iid} for iid in new_ids_to_fp],
        )

    # 入队 CUE 切轨任务
    if cue_pairs:
        queue.enqueue_many(
            "cue_split",
            [{"cue": str(cue), "src_audio": str(src)} for cue, src in cue_pairs],
        )
        log.info("scan: 已排 %d 个 cue_split 任务", len(cue_pairs))

    # 入队压缩档解压任务（zip/rar/7z）— 跨所有 root
    archives_found: list = []
    for r in roots:
        archives_found.extend(archive.detect_archives(r))
    if archives_found:
        queue.enqueue_many(
            "archive_extract",
            [{"archive": str(a)} for a in archives_found],
        )
        log.info("scan: 已排 %d 个 archive_extract 任务", len(archives_found))

    # 顺手清掉指向已不存在文件的 stale items (用户可能在 scan 之外删过文件)
    # 多 root: 相对 path 在任一 root 下能找到就算 OK, 都找不到才删
    stale_removed = 0
    for it in list(lib.items()):
        try:
            raw = it.path
            if isinstance(raw, (bytes, memoryview)):
                p = Path(bytes(raw).decode("utf-8", errors="replace"))
            else:
                p = Path(str(raw))
            abs_p = s.to_abs(p)
            if not abs_p.exists():
                it.remove()
                stale_removed += 1
        except Exception:  # noqa: BLE001
            continue
    if stale_removed:
        log.info("scan: 顺手清掉 %d 个指向不存在文件的 stale items", stale_removed)

    # 排一次 sidecar 同步 — worker 自己会等队列里 fingerprint/cue_split
    # 全部跑完才落 .musictidy.json, 防止写半成品
    queue.enqueue("sync_sidecars", {"trigger": "scan"})

    elapsed = time.time() - started
    log.info(
        "scan: done in %.1fs — %d scanned, %d added, %d skipped, %d failed, %d cue_split",
        elapsed, stats.scanned, stats.added, stats.skipped, stats.failed, len(cue_pairs),
    )

    return {
        "scanned": stats.scanned,
        "added": stats.added,
        "skipped": stats.skipped,
        "failed": stats.failed,
        "cue_splits_enqueued": len(cue_pairs),
        "elapsed_sec": round(elapsed, 2),
    }
