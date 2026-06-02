"""CUE + 单 FLAC 整轨 → 多个 FLAC 分轨 的核心逻辑（纯函数）.

为什么要切：
- AcoustID 指纹是按整文件算，整轨 FLAC 永远匹配不到单 recording
- 重复检测 / 完整度统计都按 recording 维度，整轨直接废
- 现代播放器 + 现代音乐分发都是分轨范式

设计：
- 这里只做 parse + split 两件事，不碰 DB、不碰队列
- 真正的「调度 + 清理 + 同步 beets」在 workers/cue_split.py
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


# ── 数据模型 ────────────────────────────────────────────────────
@dataclass
class CueTrack:
    number: int
    title: str = ""
    performer: str = ""
    start_seconds: float = 0.0
    end_seconds: float | None = None


@dataclass
class CueSheet:
    title: str = ""
    performer: str = ""
    file: str = ""  # FILE 引用的文件名
    tracks: list[CueTrack] = field(default_factory=list)


# ── 编码检测 ────────────────────────────────────────────────────
# 按 CJK 音乐场景常见度排
CUE_ENCODINGS = (
    "utf-8-sig", "utf-8",
    "shift_jis", "cp932",        # 日文
    "gb18030", "cp936", "gbk",   # 简中
    "big5",                       # 繁中
    "euc-kr", "cp949",            # 韩
    "cp1252", "iso-8859-1",       # 西文兜底
)


def _read_cue(cue_path: Path) -> str:
    raw = cue_path.read_bytes()
    # 优先 chardet（beets 依赖里已经有 chardet 同等品）
    try:
        import chardet  # type: ignore  # noqa: PLC0415

        result = chardet.detect(raw)
        if result.get("confidence", 0) > 0.7 and result.get("encoding"):
            try:
                return raw.decode(result["encoding"])
            except (UnicodeDecodeError, LookupError):
                pass
    except ImportError:
        pass

    for enc in CUE_ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _parse_time(ts: str) -> float:
    """CUE timestamp 'MM:SS:FF'（FF = frames, CD 75/s）→ seconds."""
    parts = ts.strip().split(":")
    if len(parts) != 3:
        return 0.0
    try:
        mm, ss, ff = (int(p) for p in parts)
    except ValueError:
        return 0.0
    return mm * 60 + ss + ff / 75.0


# ── 解析 ────────────────────────────────────────────────────────
def parse_cue(cue_path: Path) -> CueSheet:
    sheet = CueSheet()
    current: CueTrack | None = None

    for raw in _read_cue(cue_path).splitlines():
        line = raw.strip()
        if not line:
            continue

        m = re.match(r'^FILE\s+"([^"]+)"', line, re.IGNORECASE)
        if m and not current:
            sheet.file = m.group(1)
            continue

        m = re.match(r"^TRACK\s+(\d+)\s+AUDIO", line, re.IGNORECASE)
        if m:
            current = CueTrack(number=int(m.group(1)))
            sheet.tracks.append(current)
            continue

        m = re.match(r'^TITLE\s+"([^"]*)"', line, re.IGNORECASE)
        if m:
            if current:
                current.title = m.group(1)
            else:
                sheet.title = m.group(1)
            continue

        m = re.match(r'^PERFORMER\s+"([^"]*)"', line, re.IGNORECASE)
        if m:
            if current:
                current.performer = m.group(1)
            else:
                sheet.performer = m.group(1)
            continue

        m = re.match(r"^INDEX\s+(\d+)\s+(\d+:\d+:\d+)", line, re.IGNORECASE)
        if m and current and int(m.group(1)) == 1:
            current.start_seconds = _parse_time(m.group(2))
            continue

    # 算每首的 end = 下一首的 start；最后一首到文件结尾
    for i in range(len(sheet.tracks) - 1):
        sheet.tracks[i].end_seconds = sheet.tracks[i + 1].start_seconds

    return sheet


# ── 配对检测 ────────────────────────────────────────────────────
SPLIT_TARGET_EXTS = {".flac", ".ape", ".wav", ".tta", ".wv"}


def detect_pairs(root: Path) -> list[tuple[Path, Path]]:
    """找 (cue, source_audio) 对.

    匹配规则：
    1. CUE 的 FILE 字段对应一个真实文件（同目录）
    2. 文件后缀是无损格式
    3. 同目录里 sibling 无损文件 < 3（防止误判已分轨的目录）
    """
    pairs: list[tuple[Path, Path]] = []
    if not root.exists():
        return pairs

    for cue in root.rglob("*.cue"):
        if not cue.is_file():
            continue
        try:
            sheet = parse_cue(cue)
        except Exception as e:
            log.warning("cue: parse 失败 %s: %s", cue, e)
            continue
        if not sheet.tracks:
            continue

        src_audio: Path | None = None
        if sheet.file:
            candidate = cue.parent / sheet.file
            if candidate.exists():
                src_audio = candidate
        # CUE 里 FILE 名字常被 rip 工具改坏 —— 退回到目录里找
        if src_audio is None or src_audio.suffix.lower() not in SPLIT_TARGET_EXTS:
            losses = sorted(
                p for p in cue.parent.iterdir()
                if p.is_file() and p.suffix.lower() in SPLIT_TARGET_EXTS
            )
            # 大小最大的那个最可能是整轨
            losses.sort(key=lambda p: p.stat().st_size, reverse=True)
            src_audio = losses[0] if losses else None

        if src_audio is None:
            continue

        # 同目录已经有多个无损文件 = 大概率已切轨过
        same_format = [
            p for p in cue.parent.iterdir()
            if p.is_file() and p != src_audio
            and p.suffix.lower() == src_audio.suffix.lower()
        ]
        if len(same_format) >= 3:
            log.info("cue: %s 同目录已有 %d 个同格式文件，疑似已切，跳过",
                     cue.name, len(same_format))
            continue

        pairs.append((cue, src_audio))

    return pairs


# ── 切轨 ────────────────────────────────────────────────────────
def _safe_name(name: str, fallback: str = "track") -> str:
    cleaned = re.sub(r'[/\\:*?"<>|]', "_", name or "").strip().rstrip(".")
    return cleaned or fallback


def split_pair(
    cue_path: Path,
    src_audio: Path,
    dst_dir: Path | None = None,
) -> list[Path]:
    """按 CUE 切分音频文件，返回新生成的 FLAC 路径列表.

    使用 ffmpeg 重编码到 FLAC（无损 → 无损，bit-perfect；同时清理任何
    源格式 quirk）。新文件命名：`NN. Title.flac`，落在 dst_dir（默认源同目录）.
    """
    if dst_dir is None:
        dst_dir = src_audio.parent
    dst_dir.mkdir(parents=True, exist_ok=True)

    sheet = parse_cue(cue_path)
    if not sheet.tracks:
        raise ValueError(f"CUE 无 track: {cue_path}")

    total = len(sheet.tracks)
    out: list[Path] = []

    for t in sheet.tracks:
        title = _safe_name(t.title, fallback=f"Track {t.number:02d}")
        dst = dst_dir / f"{t.number:02d}. {title}.flac"
        # 跟源文件同名 → 加 .split 后缀避免覆盖
        if dst.resolve() == src_audio.resolve():
            dst = dst_dir / f"{t.number:02d}. {title} (split).flac"

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(src_audio),
            "-ss", f"{t.start_seconds:.3f}",
        ]
        if t.end_seconds is not None:
            cmd += ["-to", f"{t.end_seconds:.3f}"]
        cmd += [
            "-c:a", "flac", "-compression_level", "8",
            "-metadata", f"title={t.title}",
            "-metadata", f"artist={t.performer or sheet.performer}",
            "-metadata", f"album={sheet.title}",
            "-metadata", f"albumartist={sheet.performer}",
            "-metadata", f"track={t.number}/{total}",
            str(dst),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"ffmpeg 失败 track {t.number}: "
                f"{(e.stderr or e.stdout or '')[:500]}"
            ) from e

        out.append(dst)

    return out
