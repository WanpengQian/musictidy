"""艺人名本地化排序 key —— 三个地区各按当地习惯排:

- 中文 (含汉字, 非日本): 拼音 (张全归 Z, 不卡在「陈 Chan/Chen」中间)
- 日文: 汉字/假名 → 平假名, 按假名码点 = あいうえお(五十音)序
- 西文/其他: MB sort_name (拉丁, 姓在前)

不能用 MB 的 sort_name 排中文 —— 它是英文罗马字且粤/普混用不一致
(張=Cheung 又=Chang)。所以中文走 pypinyin、日文走 pykakasi, 在 server 算好,
所有客户端 (iOS / web) 统一消费同一个顺序 + 同一个索引字母。

返回 (sort_key, index_letter):
- sort_key: 排序用的可比较字符串
- index_letter: A–Z 索引条用的字母; 日文是假名行首 (あ/か/さ…); 落不到的给 '#'
"""

from __future__ import annotations

from functools import lru_cache

from pykakasi import kakasi
from pypinyin import Style, lazy_pinyin

_kks = kakasi()


def _is_han(ch: str) -> bool:
    o = ord(ch)
    return 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF


def _kana_row(ch: str) -> str | None:
    """假名 → 所属「行」的首假名 (あ/か/さ/た/な/は/ま/や/ら/わ)。非假名 None。"""
    o = ord(ch)
    if 0x30A1 <= o <= 0x30F6:      # 片假名 → 平假名
        o -= 0x60
    if not 0x3041 <= o <= 0x3094:
        return None
    if o <= 0x304A:
        return "あ"
    if o <= 0x3054:
        return "か"
    if o <= 0x305E:
        return "さ"
    if o <= 0x3069:
        return "た"
    if o <= 0x306E:
        return "な"
    if o <= 0x307D:
        return "は"
    if o <= 0x3082:
        return "ま"
    if o <= 0x3088:
        return "や"
    if o <= 0x308D:
        return "ら"
    return "わ"


def _latin(base: str) -> tuple[str, str]:
    base = base.strip().lower()
    first = base[:1].upper()
    idx = first if (first.isascii() and first.isalpha()) else "#"
    return base, idx


@lru_cache(maxsize=4096)
def compute(name: str | None, sort_name: str | None, country: str | None) -> tuple[str, str]:
    name = (name or "").strip()
    sort_name = (sort_name or "").strip()
    cc = (country or "").upper()

    # —— 日文 ——
    if cc == "JP" and name:
        hira = "".join(seg.get("hira", "") for seg in _kks.convert(name))
        rowch = next((c for c in hira if _kana_row(c)), "")
        if rowch:
            return hira.lower(), (_kana_row(rowch) or "#")
        # 拉丁名的日本艺人 (如 "ALI PROJECT") → 落到拉丁分支

    # —— 中文 (含汉字, 没被当成日本) ——
    if name and any(_is_han(c) for c in name):
        py = " ".join(lazy_pinyin(name, style=Style.NORMAL)).lower()
        if py and py[0].isascii() and py[0].isalpha():
            return py, py[0].upper()

    # —— 西文 / 其他 ——
    # 按显示名首字母排 (Céline Dion → C), 不用 MB 姓在前的 sort_name (Dion, Céline → D)。
    # 去掉开头的 The/A/An, 免得乐队都挤到 T。
    base = (name or sort_name)
    low = base.lower()
    for art in ("the ", "a ", "an "):
        if low.startswith(art):
            base = base[len(art):]
            break
    return _latin(base)
