"""ZIP / RAR / 7z 自动解压（核心逻辑，纯函数）.

为什么需要：
- 你扔的下载包多是压缩档（DTS-WAV in zip / APE in rar / 7z）
- 压缩档里的文件名常是 GBK / Shift-JIS（unar 自动检测，比 unzip 强）
- 解完原档进 trash，下一次 scan 自动抓里头的音频

设计：
- 只暴露检测 + 解压两个纯函数；调度、删源在 workers/archive_extract.py
- 解压目标 = 同目录下 `_extracted/<archive 名>/`，这样跟现有结构一致
- 已解过的（dst 非空）→ 视为完成，跳过 unar
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

ARCHIVE_EXTS = {".zip", ".rar", ".7z"}


def _partial_marker(archive: Path) -> Path:
    """半途崩溃保护：解压期间在 _extracted/ 同级放一个隐藏标记，成功才删。"""
    return archive.parent / "_extracted" / f".partial-{archive.stem}"


def detect_archives(root: Path) -> list[Path]:
    """递归找所有压缩档，跳过 _extracted/ 和隐藏目录."""
    out: list[Path] = []
    if not root.exists():
        return out
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel_parts = p.relative_to(root).parts
        except ValueError:
            continue
        # 跳过 _extracted / 隐藏（.trash, .Spotlight-V100 等）
        if any(part.startswith(".") or part == "_extracted" for part in rel_parts):
            continue
        if p.suffix.lower() in ARCHIVE_EXTS:
            out.append(p.resolve())
    return out


def extraction_dst(archive: Path) -> Path:
    """档案的解压目标 = `<archive 父目录>/_extracted/<archive 名>`."""
    return archive.parent / "_extracted" / archive.stem


def is_already_extracted(archive: Path) -> bool:
    """目标目录存在 + 非空 + 没有 .partial 标记 = 已解过."""
    if _partial_marker(archive).exists():
        return False   # 上次没解完
    dst = extraction_dst(archive)
    if not dst.exists():
        return False
    try:
        return any(dst.iterdir())
    except Exception:
        return False


def unar_available() -> bool:
    return shutil.which("unar") is not None


# 已知 archive 的 magic 前缀。每条都是 (signature, name)。
# 注意 RAR4 / RAR5 共享 'Rar!\x1a\x07' 前缀；ZIP 的 'PK\x05\x06' 是 EOCD（空 zip）也算
_ARCHIVE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"Rar!\x1a\x07", "RAR"),
    (b"PK\x03\x04", "ZIP"),
    (b"PK\x05\x06", "ZIP (empty)"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
)


def validate_magic(archive: Path) -> str | None:
    """检查 archive 头部是不是已知的压缩格式。

    返回 None 表示通过；返回字符串表示原因（worker 会原样写进 last_error）。
    设计目的：之前下载错 / 被站点反吸血改写的 "rar" 文件前 4K 都是零，
    unar 跑下来报 "Couldn't recognize the archive format."，看不出是文件
    本身就坏。这里提前用 magic byte 拦掉，错误信息能直接说 "前 4K 是 0"。
    """
    try:
        with open(archive, "rb") as f:
            head = f.read(16)
    except OSError as e:
        return f"读不了文件头: {e}"

    if not head:
        return "文件是空的"

    for sig, _name in _ARCHIVE_MAGIC:
        if head.startswith(sig):
            return None

    # 头部全零 → 大概率是断点续传 / anti-leech 改写
    if head[:8] == b"\x00" * 8:
        return ("前 16 字节全是 0x00 —— 文件可能下载残缺，或源站做了 anti-leech "
                "把头部替换；不是有效的 RAR/ZIP/7z。")

    hex_preview = head[:8].hex()
    return f"头部 magic 不认识 (前 8 字节: {hex_preview})；不是有效的 RAR/ZIP/7z。"


def extract(archive: Path) -> Path:
    """用 unar 解压. 返回 unar 实际落到的目录（一般 = extraction_dst()）.

    unar 优势：自动检测 GBK / Shift-JIS / Big5 文件名编码（解出来不乱码）.
    -q quiet, -o 输出 dir, -D 自动重命名冲突.

    崩溃保护：解压前写 .partial 标记；如果上次留下了标记，先清空残留目录再重解；
    成功后才删 .partial。这样不会把"半套文件"当成"已解过"。
    """
    if not unar_available():
        raise RuntimeError("unar 不可用. macOS: brew install unar / FreeBSD: pkg install unar")

    parent_dir = archive.parent / "_extracted"
    parent_dir.mkdir(parents=True, exist_ok=True)

    marker = _partial_marker(archive)
    dst = extraction_dst(archive)

    # 上次跑到一半崩了，先清理残留
    if marker.exists() and dst.exists():
        log.info("archive: 清理上次未完成的解压 %s", dst)
        shutil.rmtree(dst, ignore_errors=True)

    marker.write_text(f"extracting at {int(time.time())}\n")

    cmd = ["unar", "-q", "-o", str(parent_dir), "-D", str(archive)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        # 标记保留，下次会清理再重解
        raise RuntimeError(
            f"unar 失败 {archive.name}: {(e.stderr or e.stdout or '')[:500]}"
        ) from e

    # 全部成功才删标记
    marker.unlink(missing_ok=True)

    return dst
