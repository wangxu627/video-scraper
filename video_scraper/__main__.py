"""命令行入口：三阶段管线 scan -> match -> apply，以 JSON 中间文件衔接。

用法:
    python -m video_scraper scan  <根目录...> -o scan.json        # 阶段1（离线，可跨主机）
    python -m video_scraper match scan.json -o match.json         # 阶段2（联网匹配）
    python -m video_scraper apply match.json --execute            # 阶段3（实际操作，默认 dry-run）
    python -m video_scraper all <根目录...>                        # 三阶段串起来跑（兼容旧用法）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .pipeline import pipeline_apply, pipeline_match, pipeline_scan
from .providers.bgm import BangumiClient, BangumiError
from .providers.cache import DiskCache
from .providers.tmdb import TmdbClient, TmdbError
from .subtitles import OpenSubtitlesClient, OpenSubtitlesError

CONFIG_FILE = Path.home() / ".video_scraper.json"

EPILOG = """三阶段示例（中间文件可拷到其他主机接力）:
  # ① 在任意主机离线扫描（无需 key），产出 scan.json
  python -m video_scraper scan "D:\\Movies" "E:\\Anime" -o scan.json
  # ② 拷 scan.json 到有网/有 key 的机器上匹配，产出 match.json
  python -m video_scraper match scan.json -o match.json --tmdb-key xxx
  # ③ 先 dry-run 看计划，确认后再 --execute 实际改名/下载
  python -m video_scraper apply match.json --rename --download-poster --download-subtitles
  python -m video_scraper apply match.json --rename --download-poster --download-subtitles --execute

  # 跨主机路径映射：scan 在 D:\\Movies，apply 时库在 E:\\Media\\Movies
  python -m video_scraper apply match.json --map-root "D:\\Movies=E:\\Media\\Movies" --rename --execute

  # 兼容旧用法：一条命令跑完三阶段
  python -m video_scraper all "D:\\Movies" -o report.json
"""


def _setup_stdout() -> None:
    """Windows 控制台/管道默认 GBK/cp1252，强制 UTF-8 避免中文打印失败。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def load_config() -> Dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        except Exception as e:
            print(f"[警告] 配置文件 {CONFIG_FILE} 读取失败: {e}")
    return {}


def _write_json(data: Dict[str, Any], path: Path) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="video_scraper",
        description="影视文件信息搜刮器：scan(离线扫描) -> match(在线匹配) -> apply(实际操作)",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"video_scraper {__version__}")
    sub = p.add_subparsers(dest="command", metavar="command")

    # ---------- scan ----------
    ps = sub.add_parser("scan", help="阶段1：扫描目录+解析文件名+读元信息 -> scan.json（离线，可跨主机）",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    ps.add_argument("roots", nargs="+", help="一个或多个根目录（也可直接给单个视频文件）")
    ps.add_argument("-o", "--output", default="scan.json", help="中间 JSON 输出路径")
    ps.add_argument("--min-size-mb", type=float, default=50.0, help="小于该体积视为样本/花絮跳过（默认 50）")
    ps.add_argument("--no-recursive", action="store_true", help="不递归子目录")
    ps.add_argument("--ext", action="append", default=[], help="额外视频扩展名，可多次（如 --ext .strm）")
    ps.add_argument("--limit", type=int, default=None, help="最多处理 N 个文件（调试用）")

    # ---------- match ----------
    pm = sub.add_parser("match", help="阶段2：读 scan.json 在线匹配 TMDb/Bangumi -> match.json",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    pm.add_argument("scan_file", help="阶段1 产出的 scan.json")
    pm.add_argument("-o", "--output", default="match.json", help="匹配结果 JSON 输出路径")
    pm.add_argument("--lang", default="zh-CN", help="TMDb 本地化语言（默认 zh-CN）")
    pm.add_argument("--tmdb-key", default=None,
                    help="TMDb API Key；也可用环境变量 TMDB_API_KEY 或配置文件 ~/.video_scraper.json")
    pm.add_argument("--bgm", action="store_true", help="启用 Bangumi(bgm.tv) 动漫信息源（无需 key）")
    pm.add_argument("--bgm-always", action="store_true", help="所有条目都查 Bangumi（默认仅疑似动漫时）")
    pm.add_argument("--use-imdb", action="store_true",
                    help="(实验) 用 cinemagoer 直连 IMDb 补分数/中文别名，需 pip install cinemagoer")
    pm.add_argument("--limit", type=int, default=None, help="只匹配前 N 条（调试用）")
    pm.add_argument("--cache", default=str(Path.home() / ".video_scraper_cache.json"), help="请求缓存文件路径")
    pm.add_argument("--no-cache", action="store_true")
    pm.add_argument("-v", "--verbose", action="store_true")

    # ---------- apply ----------
    pa = sub.add_parser("apply", help="阶段3：读 match.json 实际操作（改名/海报/NFO/字幕），默认 dry-run",
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    pa.add_argument("match_file", help="阶段2 产出的 match.json")
    pa.add_argument("-o", "--output", default="apply_report.json", help="操作报告 JSON 输出路径")
    pa.add_argument("--rename", action="store_true", help="按匹配结果重命名文件（需 --execute 才真正执行）")
    pa.add_argument("--no-rename-dirs", action="store_true", help="重命名时不动目录（默认目录+文件都改）")
    pa.add_argument("--movie-template", default="{title} ({year})", help="电影名模板，字段 {title}/{year}/{original_title}")
    pa.add_argument("--episode-template", default="{title} - S{ss}E{ee}{ep_suffix}",
                    help="剧集文件名模板，字段 {title}/{ss}/{ee}/{ep}/{ep_suffix}/{year}")
    pa.add_argument("--write-nfo", action="store_true", help="写 Kodi 兼容 .nfo（需 TMDb 匹配结果）")
    pa.add_argument("--download-poster", action="store_true", help="下载 poster.jpg 到视频目录")
    pa.add_argument("--download-subtitles", action="store_true", help="下载中文字幕（OpenSubtitles，需 key+账号）")
    pa.add_argument("--subtitle-langs", default="zh-cn,en", help="字幕语言优先级，逗号分隔（默认 zh-cn,en）")
    pa.add_argument("--os-key", default=None, help="OpenSubtitles API key；也可用环境变量 OPENSUBTITLES_API_KEY")
    pa.add_argument("--os-username", default=None, help="OpenSubtitles 用户名（下载字幕需要）")
    pa.add_argument("--os-password", default=None, help="OpenSubtitles 密码（下载字幕需要）")
    pa.add_argument("--map-root", action="append", default=[], metavar="旧=新",
                    help="跨主机路径映射，如 --map-root \"D:\\Movies=E:\\Media\\Movies\"，可多次")
    pa.add_argument("--base-dir", default=None, help="把所有条目根目录都替换为该目录（简单映射）")
    pa.add_argument("--execute", action="store_true", help="真正执行操作；默认 dry-run 只打印计划")
    pa.add_argument("-v", "--verbose", action="store_true")

    # ---------- all（兼容旧的一体式用法） ----------
    pall = sub.add_parser("all", help="一条命令跑完 scan+match（可选 apply），兼容旧用法",
                          formatter_class=argparse.RawDescriptionHelpFormatter)
    pall.add_argument("roots", nargs="+", help="一个或多个根目录")
    pall.add_argument("-o", "--output", default="video_info_report.json", help="匹配结果主输出（=match.json）")
    pall.add_argument("--scan-output", default="scan.json", help="阶段1 中间文件路径")
    pall.add_argument("--apply-output", default="apply_report.json", help="阶段3 操作报告路径")
    pall.add_argument("--lang", default="zh-CN")
    pall.add_argument("--tmdb-key", default=None)
    pall.add_argument("--min-size-mb", type=float, default=50.0)
    pall.add_argument("--no-recursive", action="store_true")
    pall.add_argument("--ext", action="append", default=[])
    pall.add_argument("--limit", type=int, default=None)
    pall.add_argument("--bgm", action="store_true")
    pall.add_argument("--bgm-always", action="store_true")
    pall.add_argument("--use-imdb", action="store_true")
    pall.add_argument("--rename", action="store_true", help="附带执行重命名（需 --execute）")
    pall.add_argument("--write-nfo", action="store_true")
    pall.add_argument("--download-poster", action="store_true")
    pall.add_argument("--download-subtitles", action="store_true")
    pall.add_argument("--subtitle-langs", default="zh-cn,en")
    pall.add_argument("--os-key", default=None)
    pall.add_argument("--os-username", default=None)
    pall.add_argument("--os-password", default=None)
    pall.add_argument("--execute", action="store_true")
    pall.add_argument("--cache", default=str(Path.home() / ".video_scraper_cache.json"))
    pall.add_argument("--no-cache", action="store_true")
    pall.add_argument("-v", "--verbose", action="store_true")
    return p


def _make_clients(args, cfg: Dict[str, Any], cache) -> "tuple[Optional[TmdbClient], Optional[BangumiClient], Any]":
    api_key = getattr(args, "tmdb_key", None) or os.environ.get("TMDB_API_KEY") or cfg.get("tmdb_api_key")
    tmdb = None
    if api_key:
        try:
            tmdb = TmdbClient(api_key, language=getattr(args, "lang", "zh-CN") or "zh-CN", cache=cache)
        except TmdbError as e:
            print(f"[错误] TMDb 初始化失败: {e}")
    else:
        print("提示: 未配置 TMDb API Key -> 仅离线匹配（Bangumi 需 --bgm）。")
        print("      免费申请: https://www.themoviedb.org/settings/api ；或 set TMDB_API_KEY=xxx")

    want_bgm = bool(getattr(args, "bgm", False) or getattr(args, "bgm_always", False) or tmdb is not None)
    bgm = BangumiClient(cache=cache) if want_bgm else None

    imdb = None
    if getattr(args, "use_imdb", False):
        try:
            from .providers.imdb_provider import ImdbProvider

            imdb = ImdbProvider()
        except Exception as e:
            print(f"[警告] IMDb 源不可用（{e}），跳过 --use-imdb")
    return tmdb, bgm, imdb


def _make_os_client(args, cfg: Dict[str, Any]) -> Optional[OpenSubtitlesClient]:
    key = (getattr(args, "os_key", None) or os.environ.get("OPENSUBTITLES_API_KEY")
           or cfg.get("opensubtitles_api_key"))
    if not key:
        return None
    user = (getattr(args, "os_username", None) or os.environ.get("OPENSUBTITLES_USERNAME")
            or cfg.get("opensubtitles_username") or "")
    pwd = (getattr(args, "os_password", None) or os.environ.get("OPENSUBTITLES_PASSWORD")
           or cfg.get("opensubtitles_password") or "")
    try:
        return OpenSubtitlesClient(key, username=user, password=pwd)
    except OpenSubtitlesError as e:
        print(f"[警告] OpenSubtitles 不可用: {e}")
        return None


def _print_summary(report: Dict[str, Any], out_path: Path) -> None:
    counts = report.get("counts") or report.get("status_counts") or {}
    if not counts and report.get("stage") == "scan":
        counts = {"scanned": report.get("total", 0)}
    print(f"\n完成（{report.get('stage')}）：{json.dumps(counts, ensure_ascii=False)}")
    print(f"JSON 输出: {Path(out_path).resolve()}")


def cmd_scan(args) -> int:
    report = pipeline_scan(args.roots, args.min_size_mb, args.ext,
                           not args.no_recursive, args.limit)
    _write_json(report, Path(args.output))
    print(f"共扫描到 {report['total']} 个视频文件（解析+元信息已写入中间文件）")
    for it in report["items"]:
        p = it["parsed"]
        label = p.get("title") or Path(it["file"]).name
        if p.get("season") is not None and p.get("episode") is not None:
            label += f" S{p['season']:02d}E{p['episode']:02d}"
        print(f"  - {label}")
    _print_summary(report, Path(args.output))
    print("下一步: 把该文件拷到有网络/key 的机器上执行  match " + args.output)
    return 0


def cmd_match(args) -> int:
    cfg = load_config()
    scan_report = json.loads(Path(args.scan_file).read_text(encoding="utf-8"))
    if scan_report.get("stage") != "scan":
        print(f"[错误] {args.scan_file} 不是 scan 阶段的中间文件（stage={scan_report.get('stage')!r}）")
        return 2
    cache = None if args.no_cache else DiskCache(args.cache)
    tmdb, bgm, imdb = _make_clients(args, cfg, cache)
    report = pipeline_match(scan_report, tmdb, bgm, imdb, args.bgm_always, args.limit, args.verbose)
    _write_json(report, Path(args.output))
    for it in report["items"]:
        lk = it.get("lookup") or {}
        if it["status"] == "ok":
            bg = lk.get("bgm") or {}
            t = lk.get("title_zh") or bg.get("name_cn") or lk.get("title") or "-"
            bits = [b for b in (lk.get("imdb_id"), f"★{lk['rating']}" if lk.get("rating") is not None else None,
                                "bgm" if bg else None) if b]
            print(f"  [OK] {t} | {' '.join(bits)}")
        else:
            print(f"  [{it['status']}] {Path(it['file']).name}")
    _print_summary(report, Path(args.output))
    print("下一步: python -m video_scraper apply " + args.output + " [--rename --download-poster ...] [--execute]")
    return 0


def cmd_apply(args) -> int:
    cfg = load_config()
    match_report = json.loads(Path(args.match_file).read_text(encoding="utf-8"))
    if match_report.get("stage") != "match":
        print(f"[错误] {args.match_file} 不是 match 阶段的中间文件（stage={match_report.get('stage')!r}）")
        return 2
    os_client = _make_os_client(args, cfg) if args.download_subtitles else None
    if args.download_subtitles and os_client is None:
        print("提示: 未配置 OpenSubtitles key，字幕将被跳过（https://www.opensubtitles.com/ 免费注册）")
    mode = "DRY-RUN（预览）" if not args.execute else "EXECUTE（执行）"
    print(f"阶段3 apply —— 模式: {mode}")
    report = pipeline_apply(
        match_report,
        rename=args.rename, rename_dirs=not args.no_rename_dirs,
        movie_template=args.movie_template, episode_template=args.episode_template,
        write_nfo=args.write_nfo, download_poster=args.download_poster,
        download_subtitles=args.download_subtitles, subtitle_langs=args.subtitle_langs,
        map_root=args.map_root, base_dir=args.base_dir,
        execute=args.execute, os_client=os_client, verbose=args.verbose,
    )
    _write_json(report, Path(args.output))
    for a in report["actions"]:
        mark = {"done": "[完成]", "planned": "[计划]", "skipped": "[跳过]", "error": "[错误]"}.get(a["status"], "[?]")
        print(f"  {mark} {a['action']:9} {Path(a['file']).name}"
              + (f" -> {Path(a['detail']).name}" if a["action"] == "rename" and a["detail"] and a["status"] != "error" else
                 (f" | {a['detail'][:60]}" if a["detail"] and a["action"] != "rename" else "")))
    _print_summary(report, Path(args.output))
    if not args.execute:
        print("以上为预览；确认无误后加 --execute 真正执行。")
    return 0


def cmd_all(args) -> int:
    cfg = load_config()
    cache = None if args.no_cache else DiskCache(args.cache)

    scan_report = pipeline_scan(args.roots, args.min_size_mb, args.ext,
                                not args.no_recursive, args.limit)
    _write_json(scan_report, Path(args.scan_output))
    print(f"[scan] 共 {scan_report['total']} 个文件 -> {args.scan_output}")

    tmdb, bgm, imdb = _make_clients(args, cfg, cache)
    match_report = pipeline_match(scan_report, tmdb, bgm, imdb, args.bgm_always, args.limit, args.verbose)
    _write_json(match_report, Path(args.output))
    counts: Dict[str, int] = {}
    for it in match_report["items"]:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    print(f"[match] {json.dumps(counts, ensure_ascii=False)} -> {args.output}")

    do_apply = args.rename or args.write_nfo or args.download_poster or args.download_subtitles
    if do_apply:
        os_client = _make_os_client(args, cfg) if args.download_subtitles else None
        apply_report = pipeline_apply(
            match_report,
            rename=args.rename, rename_dirs=True,
            write_nfo=args.write_nfo, download_poster=args.download_poster,
            download_subtitles=args.download_subtitles, subtitle_langs=args.subtitle_langs,
            execute=args.execute, os_client=os_client, verbose=args.verbose,
        )
        _write_json(apply_report, Path(args.apply_output))
        print(f"[apply] {json.dumps(apply_report['counts'], ensure_ascii=False)} -> {args.apply_output}"
              + ("" if args.execute else "（dry-run，加 --execute 真正执行）"))
    print(f"\nJSON 报告: {Path(args.output).resolve()}")
    return 0


def main(argv=None) -> int:
    _setup_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    return {"scan": cmd_scan, "match": cmd_match, "apply": cmd_apply, "all": cmd_all}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
