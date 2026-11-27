"""本地结构解析 —— 一个目录 → {歌手, 专辑, 年份, 曲目, 置信度, 证据}. 不联网.

思路 (见 docs/organizer-todo.md 第 1 层): 真实收藏大多已按「专辑打包」或「按歌手放」,
信息就在文件夹里, 只是散落在几处 + 编码乱:
- 目录名   `张惠妹-勇敢` / `Amei - 我可以抱你吗` / `[APE][正]张惠妹 - 勇敢` / `歌手/专辑/`
- 文件名   `01. 标题` / `1-01 歌手 - 标题` / `01.ape`
- tag      含论坛水印 `[捌零音乐论坛]张惠妹`、GBK 被当 latin-1 读的乱码 `ÕÅ»ÝÃÃ`
- CUE      PERFORMER / TITLE / 逐轨曲名
- EAC/XLD 日志  表头 `歌手 / 专辑` + 逐轨文件名 (没 tag 没文件名的 `01.ape` 全靠它)
- Picard 写过的 musicbrainz_* tag (直接就是身份)

每个来源 (目录/tag/CUE/日志/文件名) 对歌手、专辑各投一票, 按归一化 key 聚类
(简繁/大小写/标点无关, 专辑名允许前缀包含), 权重高者胜; 来源数 + 冲突决定置信度。
外部库确认 (第 3 层) 和歌手别名 (第 2 层) 在别处, 这里只做本地能确定的事。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

try:
    from opencc import OpenCC  # opencc-python-reimplemented

    _t2s: Callable[[str], str] = OpenCC("t2s").convert
except ImportError:  # 没装就不做简繁归一 (退化为只比大小写/标点)
    def _t2s(s: str) -> str:
        return s


# ── 归一化 ──────────────────────────────────────────────────────
def norm_key(s: str | None) -> str:
    """比较用 key: NFKC + 繁→简 + casefold + 只留字母数字 (CJK 算字母)。
    `張惠妹`=`张惠妹`, `A-mei`=`Amei`, `BAD BOY`=`Bad Boy`。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = _t2s(s).casefold()
    return "".join(ch for ch in s if ch.isalnum())


# ── 乱码 ────────────────────────────────────────────────────────
def _is_cjk(o: int) -> bool:
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
            or 0xF900 <= o <= 0xFAFF or 0x20000 <= o <= 0x2FFFF)


def _is_kana_hangul(o: int) -> bool:
    return 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF


def _is_suspicious(ch: str) -> bool:
    """乱码特征字符: latin-1 补充区 (GBK 当 latin-1 读), 半角片假名 (GBK 当 SJIS 读),
    C1 控制符, U+FFFD。单个 é/ö 这种西文正常, 所以只看比例。"""
    o = ord(ch)
    return (0x80 <= o <= 0xFF or 0xFF61 <= o <= 0xFF9F or o == 0xFFFD
            or (o < 0x20 and ch not in "\t\r\n"))


def _suspicious_ratio(s: str) -> float:
    chars = [c for c in s if not c.isspace()]
    if not chars:
        return 0.0
    return sum(1 for c in chars if _is_suspicious(c)) / len(chars)


def _is_common_cjk(ch: str) -> bool:
    """常用字: GB2312 或 Big5 常用字区 (A440-C67E)。错码解出来的多是生僻字。"""
    try:
        ch.encode("gb2312")
        return True
    except UnicodeEncodeError:
        pass
    try:
        b = ch.encode("big5")
        return len(b) == 2 and 0xA440 <= int.from_bytes(b, "big") <= 0xC67E
    except UnicodeEncodeError:
        return False


def _quality(s: str) -> float:
    q = 0.0
    for ch in s:
        o = ord(ch)
        if _is_suspicious(ch):
            q -= 4
        elif _is_cjk(o):
            q += 2 if _is_common_cjk(ch) else 0.3
        elif _is_kana_hangul(o):
            q += 2
        elif o < 0x80:
            q += 1
        else:
            q += 0.5
    return q


# (误读用的编码, 真实编码): 先按误读编码还原字节, 再按真实编码解
_MOJIBAKE_CHAINS = (
    ("latin-1", "gbk"), ("latin-1", "big5"), ("latin-1", "cp932"), ("latin-1", "utf-8"),
    ("cp1252", "gbk"), ("cp1252", "big5"), ("cp1252", "cp932"), ("cp1252", "utf-8"),
    ("cp932", "gbk"), ("cp932", "big5"), ("shift_jis", "gbk"),
)


def fix_mojibake(s: str) -> tuple[str, bool]:
    """GBK/Big5/SJIS 被当 latin-1/cp1252/cp932 读出来的乱码 → 还原。
    `ÕÅ»ÝÃÃ` → `张惠妹`, `ｱiｴf` 这类同理。还原不出来原样返回 (changed=False)。"""
    if not s or s.isascii() or _suspicious_ratio(s) < 0.3:
        return s, False
    base = _quality(s)
    best: tuple[float, str] | None = None
    for wrong, right in _MOJIBAKE_CHAINS:
        try:
            cand = s.encode(wrong).decode(right)
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if any(_is_suspicious(c) for c in cand):
            continue
        if not any(_is_cjk(ord(c)) or _is_kana_hangul(ord(c)) for c in cand):
            continue
        cjk = [c for c in cand if _is_cjk(ord(c))]
        if cjk and sum(1 for c in cjk if _is_common_cjk(c)) < max(len(cjk) - (len(cjk) // 10), 1):
            continue   # 冒出生僻字 = 解错了 / 原始字节已丢 (`也喧喵虂E`)
        q = _quality(cand)
        if q > base + 1 and (best is None or q > best[0]):
            best = (q, cand)
    return (best[1], True) if best else (s, False)


_CJK_LOWER_MIX = re.compile(r"[一-鿿][a-z]{1,3}(?![a-z])|(?<![a-z])[a-z]{1,3}[一-鿿]")


def looks_garbled(s: str) -> bool:
    """还原不了的坏值: 仍有乱码特征 / 带 ? 或 U+FFFD / 汉字粘着零碎小写字母 (`張瞞cf`)。"""
    if not s:
        return False
    if "�" in s or "?" in s:
        return True
    if _suspicious_ratio(s) >= 0.3:
        return True
    return bool(_CJK_LOWER_MIX.search(s))


# ── 清洗 ────────────────────────────────────────────────────────
_OPEN, _CLOSE = "[【(（{", "]】)）}"
_BRACKET = re.compile(r"[\[【(（{]([^\[\]【】()（）{}]*)[\]】)）}]")
_WATERMARK_WORD = re.compile(
    r"论坛|論壇|bbs|www\.|\.com|\.net|\.cn|\.org|音乐网|音樂網|发烧|發燒|首发|首發|"
    r"整理|分享|下载|下載|club|forum",
    re.IGNORECASE,
)
_NOISE_TOK = re.compile(
    r"^(?:ape|flac|wav|wv|wavpack|cue|mp3|aac|m4a|alac|dsd|dsf|dff|sacd|iso|tta|tak|"
    r"hi-?res|hires|lossless|web|cdda|img|image|eac|log|scans?|covers?|"
    r"\d+\s*bits?|\d+(?:\.\d+)?\s*k(?:hz)?|\d+\s*kbps|vbr|cbr|v0|v2|"
    r"24-?96|24-?192|24-?48|16-?44(?:\.1)?|"
    r"无损|無損|整轨|整軌|分轨|分軌|镜像|鏡像|正|首版|原版|日版|港版|台版|美版|欧版|歐版|"
    r"限量|限量版|限定|限定版|初回限定盤|完整版|remaster(?:ed)?|"
    r"\d*cd\d*|disc\s*\d*|disk\s*\d*)$",
    re.IGNORECASE,
)
_YEAR_ONLY = re.compile(r"^\s*((?:19|20)\d{2})(?:[.\-/]\d{1,2}){0,2}\s*$")


def _is_noise_group(content: str) -> bool:
    toks = [t for t in re.split(r"[\s+&/,_·]+", content.strip()) if t]
    return bool(toks) and all(_NOISE_TOK.match(t) for t in toks)


def strip_watermark(s: str) -> str:
    """去论坛水印: `[捌零音乐论坛]张惠妹` → `张惠妹`, `xxx (www.foo.com)` → `xxx`。"""
    if not s:
        return s

    def _sub(m: re.Match) -> str:
        return "" if _WATERMARK_WORD.search(m.group(1)) else m.group(0)

    s = _BRACKET.sub(_sub, s)
    s = re.sub(r"\b(?:www\.)?[\w-]+\.(?:com|net|cn|org)\b", "", s, flags=re.IGNORECASE)
    return s.strip(" -_.")


def clean_value(s: str | None) -> tuple[str, list[str]]:
    """tag / CUE 值清洗: 乱码还原 → 去水印 → 去首尾空白。返回 (值, 备注)。
    还原不了的乱码返回空串 (宁缺不错)。"""
    notes: list[str] = []
    s = (s or "").strip()
    if not s:
        return "", notes
    fixed, changed = fix_mojibake(s)
    if changed:
        notes.append(f"乱码还原 {s!r}→{fixed!r}")
        s = fixed
    w = strip_watermark(s)
    if w != s:
        notes.append(f"去水印 {s!r}→{w!r}")
        s = w
    if looks_garbled(s):
        notes.append(f"乱码丢弃 {s!r}")
        return "", notes
    return s, notes


# ── 目录名 ──────────────────────────────────────────────────────
@dataclass
class DirName:
    artist: str = ""
    album: str = ""
    year: str = ""
    whole: str = ""          # 没有分隔符时整段 (可能是专辑名也可能是歌手名)


_SPACED_SEP = re.compile(r"\s+[-–—~]+\s+|\s*[-–—]\s+|\s+[-–—]\s*|\s*_-_\s*")
_LEAD_YEAR = re.compile(r"^\s*[(\[（【]?((?:19|20)\d{2})[)\]）】]?\s*[.\-_ 、]+\s*(?=\S)")
_TRAIL_YEAR = re.compile(r"\s*[(\[（【]((?:19|20)\d{2})(?:[.\-/]\d{1,2}){0,2}[)\]）】]\s*$")


# 版本/杂项分组目录, 不是歌手: VER2 / v1 / 其他 / New Folder / 华语 …
_GROUPING_DIR = re.compile(
    r"^(?:ver(?:sion)?|v|vol|版本|其他|其它|others?|misc|new folder|新建文件夹|"
    r"music|musics|音乐|音樂|mp3|flac|ape|华语|華語|日语|日語|欧美|歐美|粤语|粵語|国语|國語|"
    r"韩语|韓語|男歌手|女歌手|乐队|樂隊|合辑|合輯|精选|精選|downloads?|下载|下載)\s*\d*$",
    re.IGNORECASE,
)


def parse_dir_name(name: str) -> DirName:
    """`[APE][正]张惠妹 - 勇敢 (2012)` → artist=张惠妹 album=勇敢 year=2012。"""
    out = DirName()
    s = unicodedata.normalize("NFKC", name).replace("_", " ").strip()

    # 括号组: 年份抽出, 格式/来源噪声/水印删掉, 其余保留 (可能是 [歌手] 或 (Live))
    lead_artist = ""

    def _grp(m: re.Match) -> str:
        c = m.group(1)
        y = _YEAR_ONLY.match(c)
        if y:
            out.year = out.year or y.group(1)
            return " "
        if _is_noise_group(c) or _WATERMARK_WORD.search(c):
            return " "
        return m.group(0)

    prev = None
    while prev != s:  # 反复剥, 处理 [APE][正] 连写
        prev = s
        s = _BRACKET.sub(_grp, s).strip()

    # 开头 [歌手] 专辑
    m = re.match(r"^[\[【]([^\]】]+)[\]】]\s*(.+)$", s)
    if m:
        lead_artist, s = m.group(1).strip(), m.group(2).strip()

    # 末尾裸噪声词: `专辑 FLAC` / `专辑 APE+CUE`
    toks = s.split()
    while len(toks) > 1 and _is_noise_group(toks[-1]):
        toks.pop()
    s = " ".join(toks)

    m = _LEAD_YEAR.match(s)
    if m:
        out.year = out.year or m.group(1)
        s = s[m.end():]
    m = _TRAIL_YEAR.search(s)
    if m:
        out.year = out.year or m.group(1)
        s = s[:m.start()]

    # 歌手《专辑》
    m = re.match(r"^(.*?)\s*[-–]?\s*[《〈]([^》〉]+)[》〉]\s*(.*)$", s)
    if m and (m.group(1).strip() or lead_artist):
        out.artist = lead_artist or m.group(1).strip(" -")
        out.album = m.group(2).strip()
        return out

    parts = [p.strip() for p in _SPACED_SEP.split(s) if p.strip()]
    if len(parts) == 1 and s.count("-") == 1:
        # 裸连字符 `张惠妹-勇敢`: 两边都 ≥2 字且至少一边含 CJK 或空格才切 (`A-mei` 不切)
        a, b = (x.strip() for x in s.split("-"))
        if len(a) >= 2 and len(b) >= 2 and any(
            _is_cjk(ord(c)) or c == " " for c in a + b
        ):
            parts = [a, b]
    # 三段里有一段是年份: `歌手 - 1998 - 专辑`
    if len(parts) >= 3:
        ys = [p for p in parts if _YEAR_ONLY.match(p)]
        if ys:
            out.year = out.year or _YEAR_ONLY.match(ys[0]).group(1)
            parts = [p for p in parts if p not in ys]
    if lead_artist:
        out.artist = lead_artist
        out.album = " - ".join(parts)
    elif len(parts) >= 2:
        out.artist = parts[0]
        out.album = " - ".join(parts[1:])
    else:
        out.whole = parts[0] if parts else ""
    return out


# ── 文件名 ──────────────────────────────────────────────────────
_FN_DISC_TRACK = re.compile(
    r"^\s*(?:cd\s*)?(\d{1,2})\s*[-_.]\s*(\d{1,3})(?:\s*[.\-_、)\]]\s*|\s+|$)(.*)$", re.I
)
_FN_TRACK = re.compile(
    r"^\s*(?:track\s*(?:no\.?)?\s*)?(\d{1,3})(?:\s*[.\-_、)\]]\s*|\s+|$)(.*)$", re.I
)


def parse_filename(stem: str) -> tuple[int, int, str]:
    """文件名 stem → (碟号|0, 轨号|0, 剩余)。`1-13 歌名`→(1,13,歌名), `01. 歌名`→(0,1,歌名)。"""
    stem = unicodedata.normalize("NFKC", stem)
    m = _FN_DISC_TRACK.match(stem)
    if m and 1 <= int(m.group(1)) <= 20:
        return int(m.group(1)), int(m.group(2)), m.group(3).strip()
    m = _FN_TRACK.match(stem)
    if m:
        return 0, int(m.group(1)), m.group(2).strip()
    return 0, 0, stem.strip()


_DISC_IN_NAME = re.compile(r"(?:^|[^a-z])(?:cd|disc|disk)\s*[-_.]?\s*0*(\d{1,2})(?![0-9])", re.I)


def disc_from_path(p: Path, album_dir: Path) -> int:
    """路径 (专辑目录到文件之间) 里的碟号: `CD2/`、`最红 CD3_tracks/`。没有则 0。"""
    try:
        parts = p.parent.relative_to(album_dir).parts
    except ValueError:
        return 0
    for part in reversed(parts):
        m = _DISC_IN_NAME.search(part)
        if m:
            return int(m.group(1))
    return 0


# ── tag ─────────────────────────────────────────────────────────
_TAG_ALIASES = {
    "title": ("title",),
    "artist": ("artist",),
    "album": ("album",),
    "albumartist": ("albumartist", "album artist", "album_artist"),
    "tracknumber": ("tracknumber", "track"),
    "discnumber": ("discnumber", "disc"),
    "date": ("date", "year", "originaldate"),
    "mb_albumid": ("musicbrainz_albumid", "musicbrainz album id"),
    "mb_rgid": ("musicbrainz_releasegroupid", "musicbrainz release group id"),
    "mb_albumartistid": ("musicbrainz_albumartistid", "musicbrainz album artist id"),
}


def read_tags(path: Path) -> tuple[dict[str, str], float]:
    """统一读 tag → {title, artist, album, albumartist, tracknumber, discnumber, date, mb_*}
    + 时长秒。读不了返回 ({}, 0)。"""
    try:
        import mutagen  # noqa: PLC0415

        m = mutagen.File(str(path), easy=True)
        if m is None:
            return {}, 0.0
        length = float(getattr(m.info, "length", 0) or 0)
        raw: dict[str, str] = {}
        if m.tags:
            for k, v in m.tags.items():
                val = v[0] if isinstance(v, list) and v else v
                raw[str(k).lower()] = str(val)
        out: dict[str, str] = {}
        for key, names in _TAG_ALIASES.items():
            for n in names:
                if raw.get(n):
                    out[key] = raw[n]
                    break
        return out, length
    except Exception as e:  # noqa: BLE001
        log.debug("read_tags %s: %s", path, e)
        return {}, 0.0


def _int_of(v: str | None) -> int:
    m = re.match(r"\s*(\d+)", v or "")
    return int(m.group(1)) if m else 0


def _total_of(v: str | None) -> int:
    m = re.match(r"\s*\d+\s*/\s*(\d+)", v or "")
    return int(m.group(1)) if m else 0


# ── EAC / XLD 日志 ──────────────────────────────────────────────
@dataclass
class RipLog:
    artist: list[str] = field(default_factory=list)   # 双语表头拆出来的干净部分
    album: list[str] = field(default_factory=list)
    tracks: dict[int, str] = field(default_factory=dict)   # 轨号 → 曲名
    toc: list[tuple[int, int]] = field(default_factory=list)   # (起始扇区, 结束扇区)


def _decode_text(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    best: tuple[float, str] | None = None
    for enc in ("utf-8", "big5", "gbk", "cp932", "cp1252"):
        try:
            t = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        q = _quality(t)
        if best is None or q > best[0]:
            best = (q, t)
    return best[1] if best else raw.decode("utf-8", errors="replace")


def _clean_parts(v: str) -> list[str]:
    """`張惠妹 - A-mei Cheung` → ['張惠妹', 'A-mei Cheung']; 坏的那半丢掉。"""
    parts = [p.strip() for p in re.split(r"\s+-\s+", v) if p.strip()]
    good = [p for p in parts if not looks_garbled(p)]
    if len(parts) > 1 and not looks_garbled(v):
        good = [v.strip()] + good
    return good


_TOC_LINE = re.compile(r"^\s*(\d+)\s*\|\s*[\d:.]+\s*\|\s*[\d:.]+\s*\|\s*(\d+)\s*\|\s*(\d+)\s*$")


def parse_rip_log(path: Path) -> RipLog | None:
    try:
        text = _decode_text(path.read_bytes()[:400_000])
    except OSError:
        return None
    if not re.search(r"extraction logfile|X Lossless Decoder|Exact Audio Copy", text, re.I):
        return None
    out = RipLog()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.search(r"extraction logfile", line, re.I):
            for nxt in lines[i + 1:i + 6]:
                if " / " in f" {nxt.strip()} ":
                    a, _, b = f" {nxt.strip()} ".partition(" / ")
                    out.artist, out.album = _clean_parts(a.strip()), _clean_parts(b.strip())
                    break
            break
    for m in re.finditer(r"Track\s+(\d+)\s*[\r\n]+\s*Filename\s+(.+)", text):
        base = re.split(r"[\\/]", m.group(2).strip())[-1]
        stem = base.rsplit(".", 1)[0].strip(" -_.")
        _, _, rest = parse_filename(stem)
        rest, _ = clean_value(rest)
        if rest and not rest.isdigit():
            out.tracks[int(m.group(1))] = rest
    for line in lines:
        m = _TOC_LINE.match(line)
        if m:
            out.toc.append((int(m.group(2)), int(m.group(3))))
    return out


# ── CUE (宽松, 只读元数据) ────────────────────────────────────
@dataclass
class LooseCue:
    performer: str = ""
    title: str = ""
    tracks: dict[int, str] = field(default_factory=dict)


def parse_cue_loose(path: Path) -> LooseCue:
    """比 cuesplit.parse_cue 宽松: 值可以不带引号, TRACK 行可以是 `TRACK 01-歌名 AUDIO`。
    拆轨仍走 cuesplit (要精确时间); 这里只要歌手/专辑/曲名。"""
    from app.cuesplit import _read_cue  # noqa: PLC0415

    out = LooseCue()
    cur: int | None = None
    for raw in _read_cue(path).splitlines():
        line = raw.strip()
        m = re.match(r"^TRACK\s+0*(\d+)", line, re.I)
        if m:
            cur = int(m.group(1))
            continue
        m = re.match(r'^(TITLE|PERFORMER)\s+"?(.*?)"?\s*$', line, re.I)
        if not m:
            continue
        key, val = m.group(1).upper(), m.group(2).strip()
        if cur is None:
            if key == "TITLE":
                out.title = out.title or val
            else:
                out.performer = out.performer or val
        elif key == "TITLE" and val:
            out.tracks.setdefault(cur, val)
    return out


# ── 说明文件 (intro.txt / info.nfo): `专辑名称:勇敢` `歌手：张惠妹` ─────────
_INFO_KEYS = {
    "album": r"专辑名称|專輯名稱|专辑名|專輯名|专辑|專輯|album|title|アルバム",
    "artist": r"歌手|演唱者|演唱|艺人|藝人|艺术家|藝術家|歌手名称|artist|performer|アーティスト",
    "year": r"发行时间|發行時間|发行日期|發行日期|发行|發行|出版时间|出版日期|year|date|release date|発売日",
}


_DATE_RE = re.compile(r"((?:19|20)\d{2})(?:\s*[-./年]\s*(\d{1,2})(?:\s*[-./月]\s*(\d{1,2}))?)?")


def norm_date(s: str | None) -> str:
    """`2003年06月27日` / `2009-03-27` / `2004` / `2003.6` → `YYYY[-MM[-DD]]`; 认不出返回空。"""
    m = _DATE_RE.search(s or "")
    if not m:
        return ""
    y, mo, d = m.group(1), m.group(2), m.group(3)
    if mo and 1 <= int(mo) <= 12:
        if d and 1 <= int(d) <= 31:
            return f"{y}-{int(mo):02d}-{int(d):02d}"
        return f"{y}-{int(mo):02d}"
    return y


def parse_info_text(path: Path) -> dict[str, str]:
    try:
        raw = path.read_bytes()[:64_000]
    except OSError:
        return {}
    text = _decode_text(raw)
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*[【\[]?\s*([^:：】\]]{1,12}?)\s*[】\]]?\s*[:：]\s*(.+?)\s*$", line)
        if not m:
            continue
        k, v = m.group(1).strip().casefold(), m.group(2).strip()
        for fld, pat in _INFO_KEYS.items():
            if fld not in out and re.fullmatch(pat, k, re.I):
                if fld == "year":
                    d = norm_date(v)
                    if d:
                        out[fld] = d          # 可能带月日: 2003-06-27
                else:
                    v, _ = clean_value(v)
                    if v and len(v) <= 80:
                        out[fld] = v
    return out


# ── 投票 ────────────────────────────────────────────────────────
# 来源权重: 目录名是收藏者亲手起的, 跟 tag/CUE 同级; 日志/父目录/文件名辅助
W_TAG, W_CUE, W_DIR, W_LOG, W_INFO, W_PARENT, W_FNAME, W_VA = (
    3.0, 3.0, 2.5, 2.0, 2.0, 1.5, 1.5, 2.0)

SOURCE_LABEL = {
    "tag": "tag", "cue": "CUE", "dir": "目录名", "log": "抓轨日志", "info": "说明文件",
    "parent": "上级目录", "fname": "文件名", "va": "多歌手推断",
}


@dataclass
class _Vote:
    source: str
    weight: float
    values: list[str]          # [0] 用于显示, 其余是同义写法 (双语表头)


@dataclass
class _Cluster:
    display: str
    weight: float
    sources: list[str]
    keys: set[str]


def _keys_match(a: set[str], b: set[str], prefix: bool) -> bool:
    for x in a:
        for y in b:
            if x == y:
                return True
            if prefix and min(len(x), len(y)) >= 2 and (x.startswith(y) or y.startswith(x)):
                return True
    return False


def _vote(votes: list[_Vote], key: Callable[[str], str], prefix: bool) -> list[_Cluster]:
    """按 key 聚类, 权重降序返回。prefix=True 时 `我可以抱你吗` 与 `我可以抱你嗎？愛人`
    算同一张 (显示取权重最高那一票, 通常是 tag 的完整名)。"""
    clusters: list[_Cluster] = []
    for v in sorted(votes, key=lambda x: -x.weight):
        ks = {key(x) for x in v.values if key(x)}
        if not ks:
            continue
        hit = next((c for c in clusters if _keys_match(c.keys, ks, prefix)), None)
        if hit:
            hit.weight += v.weight
            if v.source not in hit.sources:
                hit.sources.append(v.source)
            hit.keys |= ks
        else:
            clusters.append(_Cluster(v.values[0], v.weight, [v.source], set(ks)))
    clusters.sort(key=lambda c: -c.weight)
    return clusters


# ── 结果 ────────────────────────────────────────────────────────
@dataclass
class ParsedTrack:
    path: str
    disc: int
    track: int
    title: str
    title_source: str          # tag / cue / log / fname / ""
    artist: str = ""           # 曲目级歌手 (合辑时有用)
    duration: float = 0.0
    fmt: str = ""


@dataclass
class ParsedAlbum:
    dir: str
    kind: str                  # album / artist_folder (按歌手放的散曲拆出来的一组)
    artist: str
    album: str
    year: str
    confidence: str            # high / medium / low
    tracks: list[ParsedTrack]
    artist_votes: list[dict[str, Any]]
    album_votes: list[dict[str, Any]]
    notes: list[str]
    mb: dict[str, str] = field(default_factory=dict)   # Picard tag 带的 MB 身份
    toc: list[tuple[int, int]] = field(default_factory=list)
    # 年份候选, 每个来源各报一个: [{date: YYYY[-MM[-DD]], source: tag/目录名/说明文件/…}]
    dates: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── 主入口 ──────────────────────────────────────────────────────
@dataclass
class _FileInfo:
    path: Path
    tags: dict[str, str]
    duration: float
    fn_disc: int
    fn_track: int
    fn_rest: str
    path_disc: int


def _mode(values: list[str], key: Callable[[str], str]) -> tuple[str, float]:
    """众数 (按 key 归并) + 占比。"""
    vals = [v for v in values if v and key(v)]
    if not vals:
        return "", 0.0
    c = Counter(key(v) for v in vals)
    k, n = c.most_common(1)[0]
    disp = next(v for v in vals if key(v) == k)
    return disp, n / len(vals)


def _find_cues(album_dir: Path) -> list[Path]:
    out: list[Path] = []
    for p in album_dir.rglob("*"):
        if p.suffix.lower() == ".cue" and p.is_file():
            try:
                if len(p.relative_to(album_dir).parts) <= 3:
                    out.append(p)
            except ValueError:
                pass
    return sorted(out)


def _find_logs(album_dir: Path) -> list[Path]:
    out: list[Path] = []
    for p in album_dir.rglob("*"):
        if p.suffix.lower() in (".log", ".txt") and p.is_file():
            try:
                if len(p.relative_to(album_dir).parts) <= 3 and p.stat().st_size < 400_000:
                    out.append(p)
            except (ValueError, OSError):
                pass
    return sorted(out)


def parse_folder(
    album_dir: Path,
    files: list[Path],
    root: Path | None = None,
    artist_key: Callable[[str], str] = norm_key,
    artist_canon: Callable[[str], str] | None = None,
) -> list[ParsedAlbum]:
    """解析一个物理专辑目录 (inbox._album_dir 折叠后的) 里的音频。

    通常返回 1 个 ParsedAlbum; 「按歌手放」的散曲目录 (目录名=歌手, 曲目 tag 属于
    多张专辑) 按 tag 专辑拆成多组。artist_key / artist_canon 由第 2 层 (别名表) 注入:
    前者让 `Amei`/`張惠妹` 聚成一票, 后者把胜出的写法换成 Library 规范名。
    """
    out = _parse_folder(album_dir, files, root, artist_key)
    if artist_canon:
        for a in out:
            c = artist_canon(a.artist) if a.artist else ""
            if c and c != a.artist:
                a.notes.insert(0, f"歌手规范名 {a.artist}→{c}")
                a.artist = c
    return out


def _parse_folder(
    album_dir: Path, files: list[Path], root: Path | None,
    artist_key: Callable[[str], str],
) -> list[ParsedAlbum]:
    notes: list[str] = []
    infos: list[_FileInfo] = []
    for p in sorted(files):
        tags, dur = read_tags(p)
        d, t, rest = parse_filename(p.stem)
        infos.append(_FileInfo(p, tags, dur, d, t, rest, disc_from_path(p, album_dir)))

    # ---- 各来源的歌手/专辑票 ----
    artist_votes: list[_Vote] = []
    album_votes: list[_Vote] = []
    year_votes: Counter[str] = Counter()

    # tag: 每个字段先清洗, 目录内取众数 = 一票
    t_album = [clean_value(i.tags.get("album"))[0] for i in infos]
    t_aartist = [clean_value(i.tags.get("albumartist"))[0] for i in infos]
    t_artist = [clean_value(i.tags.get("artist"))[0] for i in infos]
    for i in infos:
        for fld in ("album", "albumartist", "artist"):
            notes.extend(n for n in clean_value(i.tags.get(fld))[1] if n not in notes)
    alb, alb_share = _mode(t_album, norm_key)
    aa, _ = _mode(t_aartist, artist_key)
    ar, ar_share = _mode(t_artist, artist_key)
    distinct_artists = {artist_key(x) for x in t_artist if x}
    if alb:
        album_votes.append(_Vote("tag", W_TAG, [alb]))
    if aa:
        artist_votes.append(_Vote("tag", W_TAG, [aa]))
    elif ar and ar_share >= 0.6:
        artist_votes.append(_Vote("tag", W_TAG, [ar]))
    elif len(distinct_artists) >= 3:
        artist_votes.append(_Vote("va", W_VA, ["Various Artists"]))
    dates: list[dict[str, str]] = []
    tag_dates = [norm_date(i.tags.get("date")) for i in infos]
    for d in tag_dates:
        if d:
            year_votes[d[:4]] += 1
    td, _ = _mode([d for d in tag_dates if d], lambda x: x)
    if td:
        dates.append({"date": td, "source": "tag"})

    # MB 身份 (Picard 写过的): 目录内 ≥80% 一致才算
    mb: dict[str, str] = {}
    for fld in ("mb_rgid", "mb_albumid", "mb_albumartistid"):
        vals = [i.tags.get(fld, "") for i in infos if i.tags.get(fld)]
        if vals:
            v, n = Counter(vals).most_common(1)[0]
            if n >= 0.8 * len(infos):
                mb[fld] = v

    # CUE: 专辑级一票 + 逐轨曲名表 (按 CUE 映射到碟)
    cue_titles: dict[tuple[int, int], str] = {}
    cue_track_titles_any: dict[int, str] = {}
    cue_perf, cue_title = [], []
    cues = _find_cues(album_dir)
    for cue in cues:
        try:
            sheet = parse_cue_loose(cue)
        except Exception:  # noqa: BLE001
            continue
        dm = _DISC_IN_NAME.search(cue.stem)
        cdisc = int(dm.group(1)) if dm else (1 if len(cues) == 1 else 0)
        v, _ = clean_value(sheet.performer)
        if v:
            cue_perf.append(v)
        v, _ = clean_value(sheet.title)
        if v:
            cue_title.append(re.sub(r"\s*[\[(（]?\s*(?:cd|disc)\s*\d+\s*[\])）]?\s*$", "", v, flags=re.I))
        for no, raw_t in sheet.tracks.items():
            tt, _ = clean_value(raw_t)
            if tt:
                cue_titles[(cdisc, no)] = tt
                cue_track_titles_any.setdefault(no, tt)
    v, _ = _mode(cue_perf, artist_key)
    if v:
        artist_votes.append(_Vote("cue", W_CUE, [v]))
    v, _ = _mode(cue_title, norm_key)
    if v:
        album_votes.append(_Vote("cue", W_CUE, [v]))

    # 抓轨日志
    rip: RipLog | None = None
    for lp in _find_logs(album_dir):
        rip = parse_rip_log(lp)
        if rip:
            break
    if rip:
        if rip.artist:
            artist_votes.append(_Vote("log", W_LOG, rip.artist))
        if rip.album:
            album_votes.append(_Vote("log", W_LOG, rip.album))

    # 说明文件 (非日志的 txt / nfo)
    for ip in _find_logs(album_dir) + sorted(album_dir.glob("*.nfo")):
        if rip and ip.suffix.lower() == ".log":
            continue
        info = parse_info_text(ip)
        if info.get("album") or info.get("artist"):
            if info.get("artist"):
                artist_votes.append(_Vote("info", W_INFO, [info["artist"]]))
            if info.get("album"):
                album_votes.append(_Vote("info", W_INFO, [info["album"]]))
            if info.get("year"):
                year_votes[info["year"][:4]] += 2
                dates.append({"date": info["year"], "source": "说明文件"})
            break

    # 文件名里的 `歌手 - 标题`: 同一个歌手占 ≥60% 才算一票
    fn_artists: list[str] = []
    for i in infos:
        parts = re.split(r"\s+-\s+", i.fn_rest, maxsplit=1)
        fn_artists.append(parts[0].strip() if len(parts) == 2 else "")
    fa, fa_share = _mode(fn_artists, artist_key)
    fname_artist = fa if fa and fa_share >= 0.6 else ""
    if fname_artist:
        artist_votes.append(_Vote("fname", W_FNAME, [fname_artist]))

    # 目录名 (在 source root 下才有意义; album_dir == root 时不投)
    dn = DirName()
    parent_name = ""
    if root is None or album_dir != root:
        dn = parse_dir_name(album_dir.name)
        if dn.year:
            year_votes[dn.year] += 2
            dates.append({"date": dn.year, "source": "目录名"})
        if root is not None and album_dir.parent != root and root in album_dir.parents:
            pdn = parse_dir_name(album_dir.parent.name)
            if pdn.whole and not _GROUPING_DIR.match(pdn.whole):
                parent_name = pdn.whole
    other_artist_keys = {artist_key(x) for v in artist_votes for x in v.values}
    other_album_keys = {norm_key(x) for v in album_votes for x in v.values}
    if dn.artist and dn.album:
        # 反着写的 `专辑 - 歌手`: tag 说左边是专辑右边是歌手 → 对调
        if (norm_key(dn.artist) in other_album_keys
                and artist_key(dn.album) in other_artist_keys):
            dn.artist, dn.album = dn.album, dn.artist
            notes.append("目录名是「专辑 - 歌手」顺序, 已对调")
        artist_votes.append(_Vote("dir", W_DIR, [dn.artist]))
        album_votes.append(_Vote("dir", W_DIR, [dn.album]))
    elif dn.whole:
        # 只有一段: 跟其他来源的歌手对上 → 这是「按歌手放」的目录, 否则当专辑名
        if artist_key(dn.whole) in other_artist_keys and norm_key(dn.whole) not in other_album_keys:
            artist_votes.append(_Vote("dir", W_DIR, [dn.whole]))
        else:
            album_votes.append(_Vote("dir", W_DIR, [dn.whole]))
    if parent_name:
        artist_votes.append(_Vote("parent", W_PARENT, [parent_name]))

    artist_cl = _vote(artist_votes, artist_key, prefix=False)
    album_cl = _vote(album_votes, norm_key, prefix=True)

    # ---- 「按歌手放」: 目录名=歌手, tag 专辑五花八门 → 按 tag 专辑拆 ----
    dir_is_artist = any(v.source == "dir" for v in artist_votes) and not dn.album
    tag_albums = {norm_key(a) for a in t_album if a}
    if dir_is_artist and (len(tag_albums) >= 2 or not tag_albums) and not cues:
        return _split_artist_folder(album_dir, infos, t_album, artist_cl, notes, artist_key)

    tracks = _assemble_tracks(album_dir, infos, cue_titles, cue_track_titles_any, rip,
                              fname_artist, artist_key)
    year = year_votes.most_common(1)[0][0] if year_votes else ""
    alb = ParsedAlbum(
        dir=str(album_dir), kind="album",
        artist=artist_cl[0].display if artist_cl else "",
        album=album_cl[0].display if album_cl else "",
        year=year, confidence="low", tracks=tracks,
        artist_votes=[_cl_dict(c) for c in artist_cl],
        album_votes=[_cl_dict(c) for c in album_cl],
        notes=notes, mb=mb, toc=rip.toc if rip else [], dates=dates,
    )
    if alb_share and alb_share < 0.5:
        alb.notes.append(f"tag 专辑名不统一 (众数只占 {alb_share:.0%})")
    alb.confidence = _confidence(alb, artist_cl, album_cl, infos)
    return [alb]


def _cl_dict(c: _Cluster) -> dict[str, Any]:
    return {"value": c.display, "weight": round(c.weight, 1),
            "sources": [SOURCE_LABEL.get(s, s) for s in c.sources]}


def _assemble_tracks(
    album_dir: Path, infos: list[_FileInfo],
    cue_titles: dict[tuple[int, int], str], cue_any: dict[int, str],
    rip: RipLog | None, fname_artist: str, artist_key: Callable[[str], str],
) -> list[ParsedTrack]:
    out: list[ParsedTrack] = []
    for i in infos:
        disc = (i.path_disc or _int_of(i.tags.get("discnumber")) or i.fn_disc or 1)
        track = _int_of(i.tags.get("tracknumber")) or i.fn_track
        title, src = "", ""
        v, _ = clean_value(i.tags.get("title"))
        if v:
            title, src = v, "tag"
        if not title and track:
            t = cue_titles.get((disc, track)) or cue_titles.get((0, track))
            if not t and len({k[0] for k in cue_titles}) <= 1:
                t = cue_any.get(track)
            if t:
                title, src = t, "cue"
        if not title and track and rip and rip.tracks.get(track):
            title, src = rip.tracks[track], "log"
        if not title and i.fn_rest:
            rest = i.fn_rest
            parts = re.split(r"\s+-\s+", rest, maxsplit=1)
            if len(parts) == 2 and (not fname_artist or artist_key(parts[0]) == artist_key(fname_artist)):
                rest = parts[1]
            rest, _ = clean_value(rest)
            if rest:
                title, src = rest, "fname"
        if src == "tag" and (i.tags.get("mb_rgid") or i.tags.get("mb_albumid")):
            src = "mbtag"
        tart, _ = clean_value(i.tags.get("artist"))
        out.append(ParsedTrack(
            path=str(i.path), disc=disc, track=track, title=title, title_source=src,
            artist=tart, duration=round(i.duration, 2),
            fmt=i.path.suffix.lower().lstrip("."),
        ))
    best: dict[tuple[int, int], ParsedTrack] = {}
    for t in out:
        if t.track and t.title:
            k = (t.disc, t.track)
            if k not in best or _TITLE_RANK[t.title_source] > _TITLE_RANK[best[k].title_source]:
                best[k] = t
    for t in out:
        b = best.get((t.disc, t.track)) if t.track else None
        if b and b is not t:
            t.title, t.title_source = b.title, b.title_source
    out.sort(key=lambda t: (t.disc, t.track or 999, t.path))
    return out


_TITLE_RANK = {"mbtag": 5, "tag": 4, "cue": 3, "log": 2, "fname": 1, "": 0}


def _track_problems(tracks: list[ParsedTrack], infos: list[_FileInfo]) -> list[str]:
    """曲目号完整性: 缺号 / 无号 / 缺曲名。同 (碟,轨) 多格式不算重复 (整理时挑最优)。"""
    probs: list[str] = []
    by_disc: dict[int, set[int]] = defaultdict(set)
    for t in tracks:
        if t.track:
            by_disc[t.disc].add(t.track)
    if any(not t.track for t in tracks):
        probs.append(f"{sum(1 for t in tracks if not t.track)} 首没有轨号")
    for d, nos in sorted(by_disc.items()):
        missing = sorted(set(range(1, max(nos) + 1)) - nos)
        if missing:
            probs.append(f"碟{d} 缺轨号 {missing[:8]}")
    totals = [_total_of(i.tags.get("tracknumber")) for i in infos]
    totals = [x for x in totals if x]
    if totals and len(by_disc) == 1:
        tot = Counter(totals).most_common(1)[0][0]
        have = len(next(iter(by_disc.values())))
        if have < tot:
            probs.append(f"tag 标注共 {tot} 首, 目录里只有 {have} 首")
    untitled = sum(1 for t in tracks if not t.title)
    if untitled:
        probs.append(f"{untitled} 首没有曲名")
    return probs


def _confidence(alb: ParsedAlbum, artist_cl: list[_Cluster], album_cl: list[_Cluster],
                infos: list[_FileInfo]) -> str:
    probs = _track_problems(alb.tracks, infos)
    alb.notes.extend(probs)
    if not alb.artist or not alb.album:
        alb.notes.append("缺" + ("歌手" if not alb.artist else "专辑名"))
        return "low"

    def _ok(cl: list[_Cluster], what: str) -> bool:
        w = cl[0]
        rival = cl[1] if len(cl) > 1 else None
        if rival and rival.weight >= 0.6 * w.weight:
            alb.notes.append(f"{what}有冲突: {w.display}({'+'.join(SOURCE_LABEL.get(s, s) for s in w.sources)})"
                             f" vs {rival.display}({'+'.join(SOURCE_LABEL.get(s, s) for s in rival.sources)})")
            return False
        return len(w.sources) >= 2

    if alb.mb.get("mb_rgid") or alb.mb.get("mb_albumid"):
        return "high" if not probs else "medium"
    a_ok, r_ok = _ok(album_cl, "专辑"), _ok(artist_cl, "歌手")
    if a_ok and r_ok and not probs:
        return "high"
    return "medium"


def _split_artist_folder(
    album_dir: Path, infos: list[_FileInfo], t_album: list[str],
    artist_cl: list[_Cluster], notes: list[str], artist_key: Callable[[str], str],
) -> list[ParsedAlbum]:
    """「按歌手放」目录: 按 tag 专辑名分组, 没专辑 tag 的归「未分专辑」(散曲)。"""
    artist = artist_cl[0].display if artist_cl else album_dir.name
    groups: dict[str, list[int]] = defaultdict(list)
    disp: dict[str, str] = {}
    for idx, a in enumerate(t_album):
        k = norm_key(a)
        groups[k].append(idx)
        disp.setdefault(k, a)
    out: list[ParsedAlbum] = []
    for k, idxs in groups.items():
        sub = [infos[i] for i in idxs]
        tracks = _assemble_tracks(album_dir, sub, {}, {}, None, "", artist_key)
        years = Counter(
            m.group(1) for i in sub
            if (m := re.match(r"\s*((?:19|20)\d{2})", i.tags.get("date", "")))
        )
        alb = ParsedAlbum(
            dir=str(album_dir), kind="artist_folder", artist=artist,
            album=disp[k] if k else "", year=years.most_common(1)[0][0] if years else "",
            confidence="low", tracks=tracks,
            artist_votes=[_cl_dict(c) for c in artist_cl], album_votes=[],
            notes=list(notes) + ["按歌手放的目录, 按 tag 专辑名分组"],
        )
        if not k:
            alb.notes.append("没有专辑 tag 的散曲")
            alb.confidence = "low"
        else:
            probs = _track_problems(tracks, sub)
            alb.notes.extend(probs)
            alb.confidence = "medium"
        out.append(alb)
    out.sort(key=lambda a: (a.album == "", a.album))
    return out
