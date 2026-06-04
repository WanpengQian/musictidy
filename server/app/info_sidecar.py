"""`.musictidy.json` sidecar 读写。

放在专辑目录里, 给各 worker 当 ground-truth hint 用:
  {
    "rg_mbid": "...",                          # 该目录所有 item 应绑到的 MB rg
    "artist_mbid": "...",                      # 同理, 给 tag-fallback 救
    "cue_encoding": "gb18030",                 # CUE 解码不再猜
    "expected_tracks": 10,                     # cue_split / album_split 完事检查
    "_album": "真實",                            # 人读注释 (server 忽略 _ 开头)
    "_artist": "張惠妹",
    "_notes": "..."
  }

写时机:
  - /items/{id}/bind 成功 → 把 rg_mbid 写到 item 所在 dir
  - /local-albums/{mbid}/bind-to-mb 整目录绑完 → 同理
  - 用户也可以手写 / git 维护

读时机:
  - fingerprint worker handle_fingerprint 起手第一件事
  - cue_split worker 拿不到 rg_mbid 时 fall back
  - cuesplit._read_cue 先看 cue_encoding 提示
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SIDECAR_NAME = ".musictidy.json"


def read(dir_path: Path) -> dict[str, Any] | None:
    """读 dir_path/.musictidy.json, 返回 dict 或 None (没文件 / 坏 JSON 都 None)."""
    if not dir_path or not dir_path.is_dir():
        return None
    f = dir_path / SIDECAR_NAME
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        log.warning("info_sidecar: 读 %s 失败 %s", f, e)
        return None
    if not isinstance(data, dict):
        return None
    return data


def write(dir_path: Path, fields: dict[str, Any], *, merge: bool = True) -> bool:
    """写出 sidecar. merge=True (默认) 跟现有合并; False 覆盖.

    `_` 开头的字段 (人读注释) 保留, server 不消费但也别冲掉。
    返回是否成功 (失败仅 log, 不抛 — 写文件失败不该阻塞主流程)。
    """
    if not dir_path or not dir_path.is_dir():
        return False
    f = dir_path / SIDECAR_NAME
    data: dict[str, Any] = {}
    if merge:
        existing = read(dir_path)
        if existing:
            data = dict(existing)
    data.update(fields)
    try:
        f.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return True
    except OSError as e:
        log.warning("info_sidecar: 写 %s 失败 %s", f, e)
        return False
