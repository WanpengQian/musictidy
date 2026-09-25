"""艺人名本地化排序 key。"""
from __future__ import annotations

from app.sort_keys import compute


def k(name, sort_name="", country=""):
    return compute(name, sort_name, country)


def test_chinese_pinyin_groups_zhang_at_z():
    # 「张」全归 Z, 不卡在「陈(C)」中间
    assert k("張學友", "Cheung, Jacky", "HK")[1] == "Z"
    assert k("張震嶽", "Chang, Chen Yue", "TW")[1] == "Z"
    assert k("陳慧嫻", "Chan, Priscilla", "HK")[1] == "C"
    assert k("鄧麗君", "Teng, Teresa", "")[1] == "D"


def test_chinese_sort_order():
    names = ["張學友", "陳慧嫻", "鄧麗君", "王菲"]
    keys = sorted(names, key=lambda n: k(n)[0])
    assert keys == ["陳慧嫻", "鄧麗君", "王菲", "張學友"]  # chen deng wang zhang


def test_japanese_aiueo_rows():
    assert k("玉置浩二", "Tamaki, Koji", "JP")[1] == "た"
    assert k("久保田利伸", "Kubota, Toshinobu", "JP")[1] == "か"
    assert k("日向敏文", "Hinata, Toshifumi", "JP")[1] == "は"


def test_western_latin():
    assert k("Céline Dion", "Dion, Céline", "CA")[1] == "C"   # 按显示名, 不是姓
    assert k("The Beatles", "Beatles, The", "GB")[1] == "B"  # 去 The
