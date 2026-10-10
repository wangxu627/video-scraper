#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate_movies.py — 影视库迁移归档：扫描源目录 -> 在线匹配 -> 按中文名目录搬运。

两种用法:

A) 端到端（推荐）: 从 --src 读取原始视频，扫描+TMDb 匹配后搬运到 --dst，
   生成 "{中文名} {原名}/{中文名}.mkv" 结构。
   需要 video_scraper 包及其依赖（guessit/requests），TMDb key 取自
   --tmdb-key / 环境变量 TMDB_API_KEY / ~/.video_scraper.json:

    # 在 video-scraper 仓库根目录（依赖由 uv 管）:
    uv run python migrate_movies.py --src /volume1/downloads --dst /volume1/Nas-Movie
    uv run python migrate_movies.py --src /volume1/downloads --dst /volume1/Nas-Movie --execute
    uv run python migrate_movies.py --src ... --dst ... --match-output match.json   # 留存匹配结果
    # NAS 上（已装依赖）: python3 migrate_movies.py --src ... --dst ... [--execute]

B) 兼容旧模式: 直接给现成 match JSON（file 路径来自另一台主机时用 --src-prefix 映射）:

    python3 migrate_movies.py match_v6.json                     # /Volumes/Movie/ 前缀 -> --dst
    python3 migrate_movies.py match_v6.json --execute
    python3 migrate_movies.py match.json --src-prefix ""        # JSON 里已是本机真实路径

规则（两种模式一致，默认 dry-run，加 --execute 才真正移动）:
- 只处理 status=ok 且 lookup 有效的条目；默认只处理电影（--include-tv 扩展到剧集）
- 目标文件夹: {title_zh|title} {original_title}（相同只留一个）；
  --style paren 时为 "{中文名} ({原名})"
- 视频与同目录同前缀字幕一并移入，文件名与文件夹同名（保留扩展名/字幕语言后缀）；
  剧集文件名追加 SxxExx
- 目标已存在 / 同批撞名 -> conflict 整条跳过；源与目标是同一文件 -> 跳过
- 移动后逐级清理腾空的旧目录（端到端清 --src 侧，旧模式清 --dst 侧；不动根目录本身）
"""
import argparse
import json
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CONFIG_FILE = Path.home() / ".video_scraper.json"
SUB_EXTS = {".srt", ".ass", ".ssa", ".sub", ".sup", ".vtt", ".idx"}


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
    """从 start 逐级向上删除空目录，到 stop（不含）为止；返回删除数。"""
    try:
        stop_r = stop.resolve()
    except OSError:
        return 0
    n, cur = 0, start
    while True:
        try:
            if cur.resolve() == stop_r or any(cur.iterdir()):
                break
            cur.rmdir()
        except OSError:
            break
        n += 1
        cur = cur.parent
    return n


def make_src_map(prefix: str, root: Path):
    """旧模式: 把 match JSON 里的路径前缀映射到当前主机；prefix 为空则原样使用。"""
    if not prefix:
        return lambda f: Path(f)
    base = Path(root)

    def m(f: str) -> Path:
        if f.startswith(prefix):
            return base / f[len(prefix):].lstrip("/\\")
        return Path(f)
    return m


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        except Exception as e:
            print(f"[警告] 配置文件 {CONFIG_FILE} 读取失败: {e}")
    return {}


# =====================================================================
# 端到端：scan + match（复用 video_scraper 管线）
# =====================================================================
def run_scan_match(args) -> "dict | None":
    try:
        from video_scraper.pipeline import pipeline_match, pipeline_scan
        from video_scraper.providers.bgm import BangumiClient
        from video_scraper.providers.cache import DiskCache
        from video_scraper.providers.tmdb import TmdbClient, TmdbError
    except ImportError as e:
        print("[错误] 端到端模式需要 video_scraper 包及其依赖（guessit/requests）。")
        print("       请在 video-scraper 仓库根目录运行: uv run python migrate_movies.py ...")
        print(f"       （导入失败: {e}）")
        return None

    cfg = load_config()
    api_key = args.tmdb_key or os.environ.get("TMDB_API_KEY") or cfg.get("tmdb_api_key")
    if not api_key:
        print("[错误] 端到端模式需要 TMDb API Key：--tmdb-key / 环境变量 TMDB_API_KEY"
              " / ~/.video_scraper.json 的 tmdb_api_key")
        return None
    cache = None if args.no_cache else DiskCache(args.cache)
    try:
        tmdb = TmdbClient(api_key, language=args.lang, cache=cache)
    except TmdbError as e:
        print(f"[错误] TMDb 初始化失败: {e}")
        return None
    bgm = BangumiClient(cache=cache)  # 动漫补中文名，与 CLI match 行为一致

    print(f"[scan] 扫描 {args.src} ...")
    scan_report = pipeline_scan([args.src], args.min_size_mb, [], not args.no_recursive, args.limit)
    print(f"[scan] 共 {scan_report['total']} 个视频文件")
    if args.scan_output:
        Path(args.scan_output).write_text(
            json.dumps(scan_report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[scan] JSON: {args.scan_output}")

    print("[match] TMDb 匹配中（结果有磁盘缓存，重复跑不重复请求）...")
    match_report = pipeline_match(scan_report, tmdb, bgm, None, args.bgm_always, args.limit, args.verbose)
    print(f"[match] {json.dumps(match_report.get('counts') or {}, ensure_ascii=False)}")
    if args.match_output:
        Path(args.match_output).write_text(
            json.dumps(match_report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[match] JSON: {args.match_output}")
    return match_report


# =====================================================================
# 搬运计划 + 执行（两种模式共用）
# =====================================================================
def build_plan(items, src_map, dst_root: Path, style: str, include_tv: bool):
    planned: list = []          # (src, dst)
    conflicts: list = []
    skipped: Counter = Counter()
    seen_dst: dict = {}

    for it in items:
        if it.get("status") != "ok":
            skipped[f"status={it.get('status')}"] += 1
            continue
        lk = it.get("lookup") or {}
        if lk.get("error") or not lk.get("tmdb_id"):
            skipped["lookup 无效"] += 1
            continue
        ptype = lk.get("type") or ("tv" if (it.get("parsed") or {}).get("type") == "episode" else "movie")
        if ptype != "movie" and not include_tv:
            skipped["剧集（未开 --include-tv）"] += 1
            continue

        src = src_map(str(it.get("file") or ""))
        if not src.exists():
            skipped["源文件不存在"] += 1
            print(f"[skip] 源不存在: {src}")
            continue

        name = folder_name(lk, style)
        if ptype == "tv":
            parsed = it.get("parsed") or {}
            s, e = parsed.get("season"), parsed.get("episode")
            if s is None or e is None:
                skipped["剧集缺季集号"] += 1
                continue
            base = f"{name} S{int(s):02d}E{int(e):02d}"
        else:
            base = name

        dst_dir = dst_root / name
        dst = dst_dir / f"{base}{src.suffix}"
        try:
            if src.exists() and dst.exists() and src.resolve() == dst.resolve():
                skipped["已在目标位置"] += 1
                continue
        except OSError:
            pass
        key = str(dst).lower()
        if key in seen_dst or dst.exists():
            conflicts.append((src, dst))
            skipped["目标撞名/已存在 conflict"] += 1
            continue
        seen_dst[key] = str(src)
        planned.append((src, dst))

        for sub, rest in find_subtitles(src):
            planned.append((sub, dst_dir / f"{base}{rest}"))

    return planned, conflicts, skipped


def print_plan(planned, conflicts, skipped, dst_root: Path, style: str, execute: bool) -> None:
    videos = sum(1 for s, d in planned if d.suffix.lower() not in SUB_EXTS)
    subs = len(planned) - videos
    print("=" * 78)
    mode = "EXECUTE" if execute else "DRY-RUN（加 --execute 才会真正移动）"
    print(f"模式: {mode}   dst: {dst_root}   style: {style}")
    print(f"计划: {len(planned)} 个文件（视频 {videos} + 字幕 {subs}），"
          f"跳过 {sum(skipped.values())}，conflict {len(conflicts)}")
    print(f"跳过明细: {dict(skipped)}")
    print("=" * 78)
    for src, dst in planned:
        print(f"  {src}")
        print(f"    -> {dst}")
    for src, dst in conflicts:
        print(f"  [conflict] {src}  目标已被占用: {dst}")


def execute_plan(planned, prune_root: Path):
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
    # 清理腾空的旧目录（从深到浅，只删 prune_root 子树内的空目录）
    pruned = 0
    try:
        stop = prune_root.resolve()
    except OSError:
        stop = prune_root
    for d in sorted(moved_dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            if stop in d.resolve().parents:
                pruned += prune_empty_dirs(d, prune_root)
        except OSError:
            pass
    return done, err, pruned


def main() -> int:
    ap = argparse.ArgumentParser(
        description="影视库迁移归档：扫描/匹配/搬运到中文名目录（默认 dry-run）")
    ap.add_argument("match_json", nargs="?", default=None,
                    help="（旧模式）现成 match JSON 路径；与 --src 二选一")
    ap.add_argument("--src", default=None,
                    help="（端到端）源目录：扫描+TMDb 匹配后搬运到 --dst")
    ap.add_argument("--dst", "--root", dest="dst", default="/home/rabbitlbj/Nas-Movie",
                    help="目标媒体库根目录（旧模式下同时作为源路径映射目标）（默认 %(default)s）")
    ap.add_argument("--src-prefix", default="/Volumes/Movie/",
                    help="（仅旧模式）match JSON 里的源路径前缀；传空串表示无需映射")
    ap.add_argument("--execute", action="store_true", help="实际执行移动（默认只打印计划）")
    ap.add_argument("--include-tv", action="store_true", help="同时处理剧集（文件名带 SxxExx）")
    ap.add_argument("--style", choices=["plain", "paren"], default="plain",
                    help="文件夹名风格: plain='中文名 原名' / paren='中文名 (原名)'")
    # 端到端匹配选项
    ap.add_argument("--lang", default="zh-CN", help="TMDb 本地化语言（默认 %(default)s）")
    ap.add_argument("--tmdb-key", default=None, help="TMDb API Key（默认取环境变量/配置文件）")
    ap.add_argument("--bgm-always", action="store_true", help="所有条目都查 Bangumi（默认仅疑似动漫时）")
    ap.add_argument("--min-size-mb", type=float, default=50.0, help="小于该体积视为样本跳过（默认 50）")
    ap.add_argument("--no-recursive", action="store_true", help="不递归子目录")
    ap.add_argument("--limit", type=int, default=None, help="最多处理 N 个文件（调试用）")
    ap.add_argument("--cache", default=str(Path.home() / ".video_scraper_cache.json"),
                    help="匹配请求缓存文件路径")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--scan-output", default=None, help="（可选）保存 scan 中间 JSON 到该路径")
    ap.add_argument("--match-output", default=None, help="（可选）保存 match 中间 JSON 到该路径，"
                                                          "之后可用旧模式重放搬运")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.match_json and args.src:
        ap.error("match JSON 和 --src 二选一")
    if not args.match_json and not args.src:
        ap.error("需要 --src（端到端模式）或 match JSON（旧模式）")
    if args.src and not Path(args.src).exists():
        ap.error(f"--src 路径不存在: {args.src}")

    dst_root = Path(args.dst)
    if args.src:
        match_report = run_scan_match(args)
        if match_report is None:
            return 2
        items = match_report.get("items") or []
        src_map = make_src_map("", dst_root)   # 端到端：file 就是本机真实路径
        prune_root = Path(args.src)
    else:
        try:
            m = json.loads(Path(args.match_json).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"[错误] 读取 match JSON 失败: {e}")
            return 2
        items = m.get("items") or []
        src_map = make_src_map(args.src_prefix, dst_root)
        prune_root = dst_root

    planned, conflicts, skipped = build_plan(items, src_map, dst_root, args.style, args.include_tv)
    print_plan(planned, conflicts, skipped, dst_root, args.style, args.execute)

    if not args.execute:
        print("\n以上为预览。确认无误后加 --execute 真正执行。")
        return 0

    done, err, pruned = execute_plan(planned, prune_root)
    print(f"\n完成: 移动 {done} 个文件，失败 {err}，清理空目录 {pruned} 个。")
    return 1 if err else 0


if __name__ == "__main__":
    sys.exit(main())
