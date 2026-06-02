"""iOS 决策端 endpoints —— P2 实现.

包含：
- GET  /next                      —— 随机抽一首待决策
- GET  /stream/{item_id}          —— 转码流式
- POST /decide/{item_id}          —— 留 / 删 / 改 tag / 归档
- POST /undo/{undo_token}         —— 5 秒内撤销
"""

from fastapi import APIRouter

router = APIRouter()

# TODO P2.
