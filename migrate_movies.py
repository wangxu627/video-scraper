#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate_movies.py — 根据 match_v6.json 对电影进行归档移动+重命名（一次性工具）。

用法（在 NAS 上）:
    python3 migrate_movies.py match_v6.json                  # dry-run：只打印计划
    python3 migrate_movies.py match_v6.json --execute        # 实际执行移动
    python3 migrate_movies.py match_v6.json --execute --include-tv   # 连剧集一起（文件名带 SxxExx）

规则:
- 源路径: items[].file 中的 /Volumes/Movie/ 前缀替换为 --root（默认 /home/rabbitlbj/Nas-Movie）
- 只处理 status=ok 且 lookup 有效的条目；默认只处理电影（--include-tv 扩展到剧集）
- 目标文件夹: {title_zh|title} {original_title} 组合（两者相同则只留一个）；
  --style paren 时为 "{中文名} ({原名})"
- 视频文件与同名字幕（同目录、以视频名开头的字幕文件）一并移入文件夹，
  文件名与文件夹同名（保留原扩展名与字幕语言后缀）；剧集文件名追加 SxxExx
- 同批多个文件规划到同一目标路径时，第一个照常、其余整条跳过并标 conflict
- 移动完成后逐级清理腾空的旧子目录（不会动 --root 本身）
"""
import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SUB_EXTS = {".srt", ".ass", ".ssa", ".sub", ".sup", ".vtt", ".idx", ".srt"}


def safe_name(s: str) -> str:
    """清理 Linux/Windows 文件名中的非法字符与首尾空白。"""
    out = str(s or "").replace("/", " ").replace("\x00", " ").strip(" .")
    return out or "Unknown"


def folder_name(lk: dict, style: str) -> str:
    """主名=title_zh（缺则 title），副名=original_title；相同只留一个。"""
    zh = (lk.get("title_zh") or lk.get("title") or "").strip()
    orig = (lk.get("original_title") or "").strip()
    if not zh:
        zh = orig
    if not orig or orig == zh or not zh:
        return safe_name(zh)
    if style == "paren":
        return safe_name(f"{zh} ({orig})")
    return safe_name(f"{zh} {orig}")


def map_src(file_field: str, src_prefix: str, root: str) -> Path:
    if file_field.startswith(src_prefix):
        return Path(root.rstrip("/")) / file_field[len(src_prefix):]
    return Path(file_field)


def find_subtitles(src: Path):
    """同目录下以视频名为前缀的字幕文件，返回 [(path, 保留的后缀部分)]。"""
    out = []
    try:
        entries = list(src.parent.iterdir())
    except OSError:
        return out
    for f in entries:
        if not f.is_file() or f == src:
            continue
        if f.suffix.lower() not in SUB_EXTS:
            continue
        rest = f.name[len(src.stem):]
        if rest and (rest.startswith(".") or rest == f.suffix):
            out.append((f, rest))
    return out


def prune_empty_dirs(start: Path, stop: Path) -> int:
    """从 start 逐级向上删除空目录，到 stop（含）为止；返回删除数。"""
    n = 0
    cur = start
    try:
        stop_resolved = stop.resolve()
    except OSError:
        return 0
    while cur != stop and stop_resolved in cur.resolve().parents or cur == stop:
        try:
            if cur == stop or any(cur.iterdir()):
                break
            cur.rmdir()
            n += 1
            cur = cur.parent
        except OSError:
            break
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="按 match JSON 归档移动电影（默认 dry-run）")
    ap.add_argument("match_json", help="match_v6.json 路径")
    ap.add_argument("--execute", action="store_true", help="实际执行移动（默认只打印计划）")
    ap.add_argument("--include-tv", action="store_true", help="同时处理剧集（文件名带 SxxExx）")
    ap.add_argument("--src-prefix", default="/Volumes/Movie/", help="scan.json 里的源路径前缀")
    ap.add_argument("--root", default="/home/rabbitlbj/Nas-Movie", help="目标媒体库根目录")
    ap.add_argument("--style", choices=["plain", "paren"], default="plain",
                    help="文件夹名风格: plain='中文名 原名' / paren='中文名 (原名)'")
    args = ap.parse_args()

    m = json.loads(Path(args.match_json).read_text(encoding="utf-8"))
    root = Path(args.root)
    planned: list = []          # (src, dst)
    conflicts: list = []
    skipped: Counter = Counter()
    seen_dst: dict = {}

    for it in m.get("items", []):
        rp = it.get("relpath") or "?"
        if it.get("status") != "ok":
            skipped[f"status={it.get('status')}"] += 1
            continue
        lk = it.get("lookup") or {}
        if lk.get("error") or not lk.get("tmdb_id"):
            skipped["lookup 无效"] += 1
            continue
        ptype = lk.get("type") or ("tv" if (it.get("parsed") or {}).get("type") == "episode" else "movie")
        if ptype != "movie" and not args.include_tv:
            skipped["剧集（未开 --include-tv）"] += 1
            continue

        src = map_src(str(it.get("file") or ""), args.src_prefix, args.root)
        if not src.exists():
            skipped[f"源文件不存在"] += 1
            print(f"[skip] 源不存在: {src}")
            continue

        name = folder_name(lk, args.style)
        if ptype == "tv":
            parsed = it.get("parsed") or {}
            s, e = parsed.get("season"), parsed.get("episode")
            if s is None or e is None:
                skipped["剧集缺季集号"] += 1
                continue
            base = f"{name} S{int(s):02d}E{int(e):02d}"
        else:
            base = name

        dst_dir = root / name
        dst = dst_dir / f"{base}{src.suffix}"
        key = str(dst).lower()
        if key in seen_dst:
            conflicts.append((src, dst))
            skipped["目标撞名 conflict"] += 1
            continue
        seen_dst[key] = str(src)
        planned.append((src, dst))

        for sub, rest in find_subtitles(src):
            sub_dst = dst_dir / f"{base}{rest}"
            planned.append((sub, sub_dst))

    videos = sum(1 for s, d in planned if d.suffix.lower() not in SUB_EXTS)
    subs = len(planned) - videos
    print("=" * 78)
    mode = "EXECUTE" if args.execute else "DRY-RUN（加 --execute 才会真正移动）"
    print(f"模式: {mode}   root: {root}   style: {args.style}")
    print(f"计划: {len(planned)} 个文件（视频 {videos} + 字幕 {subs}），"
          f"跳过 {sum(skipped.values())}，conflict {len(conflicts)}")
    print(f"跳过明细: {dict(skipped)}")
    print("=" * 78)
    for src, dst in planned:
        print(f"  {src}")
        print(f"    -> {dst}")
    for src, dst in conflicts:
        print(f"  [conflict] {src}  目标已被占用: {dst}")

    if not args.execute:
        print("\n以上为预览。确认无误后加 --execute 真正执行。")
        return 0

    done = err = 0
    moved_dirs = set()
    for src, dst in planned:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            done += 1
            moved_dirs.add(src.parent)
        except OSError as e:
            err += 1
            print(f"[error] {src}: {e}")
    # 清理腾空的旧目录（从深到浅，只删 --root 内的空目录）
    pruned = 0
    for d in sorted(moved_dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            if root.resolve() in d.resolve().parents:
                pruned += prune_empty_dirs(d, root)
        except OSError:
            pass
    print(f"\n完成: 移动 {done} 个文件，失败 {err}，清理空目录 {pruned} 个。")
    return 1 if err else 0


if __name__ == "__main__":
    sys.exit(main())
