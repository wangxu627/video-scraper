"""目录扫描：收集待分析的视频文件，并附带目录层级提示（用于辅助识别片名）。"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, List, Tuple

# 常见视频容器扩展名
VIDEO_EXTS = {
    ".3g2", ".3gp", ".3gp2", ".asf", ".avi", ".divx", ".dv", ".flv", ".iso",
    ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".ogm",
    ".ogv", ".qt", ".rm", ".rmvb", ".tp", ".trp", ".ts", ".vob", ".webm", ".wmv",
}

# 跳过的系统/回收站目录
SKIP_DIR_NAMES = {"$recycle.bin", "system volume information", "lost+found", "#recycle", "recycle"}

# 跳过的样本/花絮类文件名与 macOS 垃圾文件
SKIP_FILE_PAT = re.compile(r"(?i)(^sample[._ -]|[._ -]sample[._ -]|^._)")

# 纯季目录名（此时节目名在上一级目录）
SEASON_DIR_RE = re.compile(r"^\s*(?:[Ss]eason\s*\d{1,2}|[Ss]\d{1,2}|第\s*\d+\s*季|specials?)\s*$")


def _norm_ext(e: str) -> str:
    e = e.strip().lower()
    return e if e.startswith(".") else "." + e


def _skip_dir(name: str) -> bool:
    return name.startswith(".") or name.lower() in SKIP_DIR_NAMES


def _folder_hint(p: Path, root: Path) -> str:
    """返回对识别片名有帮助的目录名；若文件直接位于根目录则返回空串。"""
    parent = p.parent
    if parent == root:
        return ""
    name = parent.name
    if SEASON_DIR_RE.match(name):
        gp = parent.parent
        return gp.name if gp != root else ""
    return name


def scan_roots(
    roots: Iterable[str],
    min_size_mb: float = 50.0,
    extra_exts: Iterable[str] = (),
    recursive: bool = True,
) -> List[Tuple[Path, str, Path]]:
    """扫描一个或多个根目录，返回 [(视频文件路径, 目录提示, 所属根), ...]。

    - min_size_mb: 小于该体积的文件视为样本/花絮跳过（0 表示不过滤）
    - roots 也可以直接给单个视频文件
    - 所属根用于生成 relpath，供跨主机执行阶段做路径映射
    """
    if min_size_mb is None:
        min_size_mb = 0.0
    exts = set(VIDEO_EXTS) | {_norm_ext(e) for e in extra_exts}
    out: List[Tuple[Path, str, Path]] = []
    min_bytes = min_size_mb * 1024 * 1024

    for root in roots:
        rootp = Path(root).expanduser()
        try:
            rootp = rootp.resolve()
        except OSError:
            pass

        # 直接给文件的情况
        if rootp.is_file():
            if rootp.suffix.lower() in exts:
                out.append((rootp, "", rootp.parent))
            else:
                print(f"[警告] 跳过非视频文件: {rootp}")
            continue
        if not rootp.is_dir():
            print(f"[警告] 路径不存在，跳过: {rootp}")
            continue

        if recursive:
            for dirpath, dirnames, filenames in os.walk(rootp):
                dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d))
                for fn in sorted(filenames):
                    if fn.startswith(".") or SKIP_FILE_PAT.search(fn):
                        continue
                    p = Path(dirpath) / fn
                    if p.suffix.lower() not in exts:
                        continue
                    try:
                        size = p.stat().st_size
                    except OSError:
                        continue
                    if size < min_bytes:
                        continue
                    out.append((p, _folder_hint(p, rootp), rootp))
        else:
            for p in sorted(rootp.iterdir()):
                if not p.is_file() or p.name.startswith(".") or SKIP_FILE_PAT.search(p.name):
                    continue
                if p.suffix.lower() not in exts:
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                if size < min_bytes:
                    continue
                out.append((p, "", rootp))

    # 多个根目录可能重叠，按路径去重
    seen = set()
    dedup: List[Tuple[Path, str, Path]] = []
    for p, h, r in out:
        k = str(p).lower()
        if k in seen:
            continue
        seen.add(k)
        dedup.append((p, h, r))
    return dedup
