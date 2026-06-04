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
    """解 CUE 的编码。

    思路: CUE 文件结构高度规则 (PERFORMER / TITLE / FILE / TRACK / INDEX),
    用「解出来的文本含多少 CUE 关键字 + 合法 CJK 字符」给每个候选编码打分,
    最高分胜出。比 chardet 准 — 后者对短文本 (CUE 一般 < 2KB) 经常瞎猜,
    而且 gb18030 对任何字节都能解出来 (虽然多数是乱码), 单纯靠"能解码"
    选不出来。

    .musictidy.json sidecar 里有 cue_encoding 字段就直接用, 不再评分。
    """
    raw = cue_path.read_bytes()

    # sidecar 优先
    try:
        from app import info_sidecar  # noqa: PLC0415
        sc = info_sidecar.read(cue_path.parent)
        if sc and isinstance(sc.get("cue_encoding"), str):
            try:
                return raw.decode(sc["cue_encoding"])
            except (UnicodeDecodeError, LookupError) as e:
                log.warning(
                    "cue: sidecar 指定 cue_encoding=%s 但解不动 %s: %s, 走评分",
                    sc["cue_encoding"], cue_path.name, e,
                )
    except ImportError:
        pass

    # chardet 当一个候选, 不再单独信它 (短文本经常误判)
    candidates = list(CUE_ENCODINGS)
    try:
        import chardet  # type: ignore  # noqa: PLC0415

        det = chardet.detect(raw)
        if det.get("encoding"):
            enc = det["encoding"].lower()
            if enc not in candidates:
                candidates.insert(0, enc)
    except ImportError:
        pass

    best: tuple[float, str] | None = None
    for enc in candidates:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        score = _score_cue_text(text)
        if best is None or score > best[0]:
            best = (score, text)

    if best is not None:
        return best[1]
    # 全部都解不动 → 兜底
    return raw.decode("utf-8", errors="replace")


# 结构关键字 (CUE spec, 都是 ASCII, 大小写不敏感)
_CUE_KEYWORDS = (
    "PERFORMER", "TITLE", "FILE", "TRACK", "INDEX",
    "REM", "GENRE", "DATE", "ISRC", "CATALOG",
)
_INDEX_RE = re.compile(r"INDEX\s+\d+\s+\d+:\d+:\d+", re.I)


def _score_cue_text(text: str) -> float:
    """给某个编码解出来的文本打分: 越高越像合法 CUE."""
    if not text:
        return -1e6

    score = 0.0
    upper = text.upper()
    for kw in _CUE_KEYWORDS:
        score += upper.count(kw) * 5.0
    # INDEX MM:SS:FF 时间戳出现一次 +3, 真 CUE 至少有几行
    score += len(_INDEX_RE.findall(text)) * 3.0

    # CJK / Hangul / Kana 合法字符比例
    cjk = 0
    bad = 0
    for ch in text:
        o = ord(ch)
        if o == 0xFFFD:
            bad += 5
            continue
        if o < 0x20 and ch not in "\r\n\t":
            bad += 2  # 不该出现的控制字符 (非换行/tab)
            continue
        if (0x4E00 <= o <= 0x9FFF      # CJK 统一汉字
            or 0x3040 <= o <= 0x30FF   # 平假名 / 片假名
            or 0xAC00 <= o <= 0xD7AF   # 谚文
            or 0xF900 <= o <= 0xFAFF   # CJK 兼容汉字
            or 0x20000 <= o <= 0x2FFFF):  # CJK 扩展
            cjk += 1
    score += cjk * 1.0
    score -= bad
    return score


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

    同 dir 多个 CUE 指同一 source audio (例如 Big5 + GBK 双 CUE) →
    只保留一个 (避免 split 两次 → 同一首歌出两份不同编码文件名).
    选哪个 CUE 优先级:
      1. sidecar 给的 artist 地区匹配 → 选名字含对应编码 hint 的 CUE
      2. 否则: 按 _score_cue_text 评分 (含 mojibake 自动扣分) 选最高分
    """
    pairs: list[tuple[Path, Path]] = []
    if not root.exists():
        return pairs

    # rglob("*.cue") 是 case-sensitive, 漏 .CUE 大写文件
    # (rip 工具老版本经常给大写). 手动 walk 一遍 case-insensitive.
    cue_files = [
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() == ".cue"
    ]
    # 先收集所有 candidate (cue, src) 对, 再 dedup by src
    candidates: list[tuple[Path, Path]] = []
    for cue in cue_files:
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

        candidates.append((cue, src_audio))

    # dedup: 多个 CUE 指同一 src_audio → 选最佳 1 个
    by_src: dict[Path, list[Path]] = {}
    for cue, src in candidates:
        by_src.setdefault(src.resolve(), []).append(cue)

    for src_key, cues in by_src.items():
        if len(cues) == 1:
            pairs.append((cues[0], src_key))
            continue
        # 多 CUE → 评分挑最优
        chosen = _pick_best_cue(cues)
        skipped = [c.name for c in cues if c != chosen]
        log.info(
            "cue: %s 多个 CUE (%s), 选 %s, 跳过 %s",
            src_key.name, ", ".join(c.name for c in cues), chosen.name, skipped,
        )
        pairs.append((chosen, src_key))

    return pairs


def _pick_best_cue(cues: list[Path]) -> Path:
    """同 source 多 CUE 时挑最佳: 评分高 + 跟艺人地区编码 hint 匹配。

    评分用 _score_cue_text (CUE 关键字 + CJK 字符正分, U+FFFD 乱码扣分).
    sidecar 给了 artist_mbid → 查 mb_artist.country → 推期望编码:
      TW/HK/MO → Big5/big5; CN → GBK/GB18030; JP → SJIS/Shift_JIS
    文件名含该 hint 的 CUE 在评分 tiebreak 加分。
    """
    expected_enc_hints = _expected_enc_hints_from_sidecar(cues[0].parent)

    def _score(cue: Path) -> float:
        try:
            text = _read_cue(cue)
        except Exception:  # noqa: BLE001
            return -1e6
        score = _score_cue_text(text)
        if expected_enc_hints:
            name_lc = cue.name.lower()
            if any(h in name_lc for h in expected_enc_hints):
                score += 50.0  # 比单字符 CJK 加分大, 但够不上压倒乱码扣分
        return score

    return max(cues, key=_score)


# 艺人 country → CUE 文件名常见编码字串 (小写 substring 匹配)
_COUNTRY_ENC_HINTS: dict[str, tuple[str, ...]] = {
    "TW": ("big5", "b5"),
    "HK": ("big5", "b5"),
    "MO": ("big5", "b5"),
    "CN": ("gbk", "gb18030", "gb"),
    "JP": ("sjis", "shift_jis", "shiftjis"),
    "KR": ("euc-kr", "euckr", "cp949"),
}


def _expected_enc_hints_from_sidecar(d: Path) -> tuple[str, ...]:
    """目录里 sidecar 给了 artist_mbid → 查 mb_artist.country → 返回期望
    编码字串 hint (用来在 CUE 文件名里 substring 匹配)。
    """
    try:
        from app import info_sidecar  # noqa: PLC0415
        sc = info_sidecar.read(d)
        artist_mbid = (sc or {}).get("artist_mbid") if sc else None
        if not artist_mbid:
            return ()
        from app.db import get_engine  # noqa: PLC0415
        from sqlalchemy import text  # noqa: PLC0415
        with get_engine().connect() as conn:
            row = conn.execute(
                text("SELECT country FROM mb_artist WHERE mbid=:m"),
                {"m": artist_mbid},
            ).first()
        country = (row.country if row else "") or ""
        return _COUNTRY_ENC_HINTS.get(country.upper(), ())
    except Exception:  # noqa: BLE001
        return ()


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
