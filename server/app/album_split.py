"""按 MB canonical track 长度 + silence detect 拆一个整张 FLAC/APE/WAV 大文件。

工作流：
1. 用户给 (item_id, rg_mbid)
2. 读 mb_release_group.tracks_json 拿 N 首 canonical track 长度
3. 算累计时间戳 = 'MB 边界'，第 k 首在 sum(lengths[:k]) 处开始
4. ffmpeg silencedetect 在源文件上扫静音段 (默认 -30dB / ≥ 1.0s)
5. 给每个 MB 边界找 ±5s 内最近的静音段中点；找不到就退回纯 MB 时长 (硬切)
6. ffmpeg -ss / -t 按调整后切点切 N 段 FLAC, 命名 'NN. Title.flac'
7. 原大文件 mv 到 .trash/manualsplit_<ts>/
8. beets 删原 item + import 新文件; fingerprint worker 后续接

不依赖外部库, 只 ffmpeg + ffprobe。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ── ffmpeg silence detect ──────────────────────────────────────
def detect_silences(src: Path, noise_db: float = -30.0, min_dur_s: float = 1.0) -> list[tuple[float, float]]:
    """跑 ffmpeg silencedetect, 返回 [(start_s, end_s), ...] 静音段。
    parse 'silence_start: X' + 'silence_end: Y' 行对。"""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(src),
        "-af", f"silencedetect=noise={noise_db}dB:d={min_dur_s}",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.exception("silencedetect failed: %s", e)
        return []

    text = proc.stderr or ""
    starts = [
        float(m.group(1))
        for m in re.finditer(r"silence_start:\s*([0-9.]+)", text)
    ]
    ends = [
        float(m.group(1))
        for m in re.finditer(r"silence_end:\s*([0-9.]+)", text)
    ]
    pairs = list(zip(starts, ends))
    return pairs


def align_split_points(
    mb_lengths: list[float],
    silences: list[tuple[float, float]],
    tolerance_s: float = 5.0,
) -> list[float]:
    """给一组 MB canonical track 长度 + 静音段, 算 N-1 个切点位置 (秒)。
    每个 MB 边界找 ±tolerance 内最近静音段中点；没找到退回 MB 边界本身。
    返回的 N 个切点是 '每首歌的起始时间' (第一个永远是 0)。
    """
    if not mb_lengths:
        return []
    # cumulative boundaries: track i 起始 = sum(lengths[:i])
    boundaries = [0.0]
    cum = 0.0
    for L in mb_lengths[:-1]:
        cum += L
        boundaries.append(cum)
    # 最后一首不需要切点 (切到 EOF)

    # 把 silences 转成中点列表 (按时间排好), 二分找最近
    silence_mids = sorted(((s + e) / 2.0 for s, e in silences))

    aligned: list[float] = [0.0]  # 第一首总是从 0 开始
    import bisect
    for b in boundaries[1:]:
        if not silence_mids:
            aligned.append(b)
            continue
        idx = bisect.bisect_left(silence_mids, b)
        candidates = []
        if idx < len(silence_mids):
            candidates.append(silence_mids[idx])
        if idx > 0:
            candidates.append(silence_mids[idx - 1])
        best = min(candidates, key=lambda x: abs(x - b))
        if abs(best - b) <= tolerance_s:
            aligned.append(best)
        else:
            aligned.append(b)
    return aligned


def split_audio(
    src: Path, out_dir: Path, splits: list[tuple[float, float, str]],
) -> list[Path]:
    """splits = [(start_s, end_s, title), ...] (end_s 可以 0 表示到末尾)
    每段 ffmpeg -ss/-to 切 FLAC, 文件名 'NN. {title}.flac'。
    返回所有新文件路径列表。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    new_paths: list[Path] = []
    for i, (start, end, title) in enumerate(splits, start=1):
        safe_title = re.sub(r"[/<>:|?*]+", "_", title).strip() or f"Track {i:02d}"
        fname = f"{i:02d}. {safe_title}.flac"
        out = out_dir / fname

        cmd = [
            "ffmpeg", "-hide_banner", "-nostats", "-y",
            "-i", str(src),
            "-ss", f"{start:.3f}",
        ]
        if end > 0:
            cmd += ["-to", f"{end:.3f}"]
        cmd += [
            "-c:a", "flac",
            "-compression_level", "5",
            str(out),
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=600, check=True)
            new_paths.append(out)
        except subprocess.CalledProcessError as e:
            log.error("ffmpeg split %d failed: %s", i, e.stderr[:200] if e.stderr else "")
            continue
    return new_paths


def split_by_album_track_lengths(
    src: Path,
    mb_tracks: list[dict[str, Any]],
    out_dir: Path,
    trash_dir: Path = None,  # 已不用; 切完直接 unlink 源, 保留参数兼容老调用
) -> tuple[list[Path], dict[str, Any]]:
    """主入口: src 是大文件, mb_tracks 来自 mb_release_group.tracks_json
    每项含 {position, title, length_s, recording_mbid}.

    返回 (new_paths, summary)。summary 含 silences_found / aligned_count 等
    给 UI 反馈用。
    """
    if not src.exists():
        raise FileNotFoundError(f"source missing: {src}")
    if not mb_tracks:
        raise ValueError("mb_tracks empty")

    # 排序确保 position 升序
    sorted_tracks = sorted(mb_tracks, key=lambda t: int(t.get("position") or 0))
    lengths = [float(t.get("length_s") or 0) for t in sorted_tracks]
    titles = [
        (t.get("title") or f"Track {i+1:02d}")
        for i, t in enumerate(sorted_tracks)
    ]

    silences = detect_silences(src)
    aligned_starts = align_split_points(lengths, silences)

    # splits = [(start, end, title), ...]
    splits: list[tuple[float, float, str]] = []
    for i, start in enumerate(aligned_starts):
        end = aligned_starts[i + 1] if i + 1 < len(aligned_starts) else 0.0
        splits.append((start, end, titles[i]))

    # 切到临时子目录, 不跟源同名碰撞
    tmp = out_dir / f".__split_{int(time.time())}"
    new_paths = split_audio(src, tmp, splits)
    if not new_paths:
        # 全部切失败, 清掉 tmp
        try:
            shutil.rmtree(tmp)
        except OSError:
            pass
        raise RuntimeError("ffmpeg 切轨全部失败")

    # 切成功直接删源大文件 (跟 cue_split 同语义, 省磁盘 + 避免 trash 堆 GB 级备份)
    try:
        src.unlink()
    except OSError as e:
        log.warning("album_split: 删源 %s 失败: %s", src, e)

    # tmp 里的新文件 mv 到目标目录
    final_paths: list[Path] = []
    for p in new_paths:
        final = out_dir / p.name
        if final.exists():
            # 同名冲突: 改名 'NN. Title (split).flac'
            final = out_dir / (p.stem + " (split)" + p.suffix)
        try:
            shutil.move(str(p), str(final))
            final_paths.append(final)
        except OSError as e:
            log.error("mv split file failed %s: %s", p, e)
    try:
        shutil.rmtree(tmp)
    except OSError:
        pass

    return final_paths, {
        "silences_found": len(silences),
        "tracks_total": len(splits),
        "splits_written": len(final_paths),
        "aligned_starts": aligned_starts,
    }
