"""sync_sidecars: mega-合辑/盒装 吸收护栏。"""

from __future__ import annotations

import pytest

from app.workers.sync_sidecars import is_incidental_mega_match


@pytest.mark.parametrize(
    ("total_tracks", "n_files", "expected"),
    [
        # —— 真实中招案例: 单张专辑被吸进 mega 合辑/盒装 → 该拒绝 ——
        (222, 44, True),   # 邓丽君 现场 → 君之頌讚四 222 轨盒装
        (123, 17, True),   # 陈慧娴[分轨] → 大盛期 123 轨
        (80, 14, True),    # 张雨生 DEMO → 10年情歌最精選 80 轨
        (92, 10, True),    # 王菲 → 從頭認識 92 轨
        # —— 正常专辑 (含残缺): 不该误伤, 仍按位置补全 ——
        (12, 3, False),    # 拥有 12 轨专辑的 3 轨 = 残缺专辑, 允许
        (30, 5, False),    # 低于 mega 阈值
        (10, 1, False),    # 单曲 / 小专辑
        # —— 真拥有整套盒装: coverage 够高 → 允许 ——
        (222, 200, False),
        (40, 30, False),   # 恰好阈值上, 拥有 75% → 允许
        # —— 阈值边界 ——
        (40, 10, True),    # 40 轨 mega, 只占 25% → 拒绝
        (39, 1, False),    # 39 < 40, 不算 mega
    ],
)
def test_is_incidental_mega_match(total_tracks, n_files, expected):
    assert is_incidental_mega_match(total_tracks, n_files) is expected
