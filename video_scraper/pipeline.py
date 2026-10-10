"""三阶段管线核心：scan -> match -> apply，以 JSON 中间文件衔接。

- scan  ：扫描目录 + guessit 解析 + MediaInfo 元信息（离线、无 key、可跨主机）
- match ：读 scan JSON，在线匹配 TMDb/Bangumi[/IMDb]，产出 match JSON（只读不写媒体库）
- apply ：读 match JSON，实际操作（改名/海报/NFO/字幕），默认 dry-run
"""
from __future__ import annotations

import os
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .mediainfo import read_media_info
from .output import download_poster_for, write_nfo_for
from .parsing import parse_video
from .providers.bgm import BangumiClient, BangumiError
from .providers.tmdb import TmdbClient, TmdbError
from .renamer import MOVIE_TEMPLATE, EPISODE_TEMPLATE, plan_rename
from .scanner import scan_roots


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# 匹配置信度阈值：TMDb 得分低于该值的条目标记为 uncertain，apply 默认跳过。
# 包含式匹配（fuzzy）得 40 分，加上热度封顶 10 分恰好 50——实测这个档位的
# "命中"几乎全是同名前缀误配（如 Evangelion 撞上任意一部剧场版），提到 55 拦下。
UNCERTAIN_BELOW = 55.0


def unique(seq):
    """去重保序、剔除空值。"""
    seen = set()
    for x in seq:
        if not x:
            continue
        k = str(x).strip().lower()
        if k and k not in seen:
            seen.add(k)
            yield str(x).strip()


# =====================================================================
# 阶段1：scan
# =====================================================================
def pipeline_scan(roots: List[str], min_size_mb: float, extra_exts: List[str],
                  recursive: bool, limit: Optional[int]) -> Dict[str, Any]:
    found = scan_roots(roots, min_size_mb=min_size_mb, extra_exts=extra_exts, recursive=recursive)
    if limit:
        found = found[:limit]
    items: List[Dict[str, Any]] = []
    for path, hint, root in found:
        try:
            relpath = path.relative_to(root).as_posix()
        except ValueError:
            relpath = path.name
        media = read_media_info(path)
        embedded = (media.get("embedded_title") or "") if isinstance(media, dict) else ""
        parsed = parse_video(path, folder_hint=hint or "", embedded_title=embedded)
        if media.get("file_size_mb") is None:
            try:
                media["file_size_mb"] = round(path.stat().st_size / 1048576, 1)
            except OSError:
                pass
        items.append({
            "file": str(path),
            "relpath": relpath,
            "root": str(root),
            "folder_hint": hint or None,
            "size_mb": media.get("file_size_mb"),
            "parsed": parsed,
            "media_info": media,
            "status": "scanned",
        })
    return {
        "tool": f"video_scraper {__version__}",
        "stage": "scan",
        "generated_at": _now(),
        "scan_params": {
            "roots": [str(r) for r in roots], "min_size_mb": min_size_mb,
            "recursive": recursive, "extra_exts": list(extra_exts),
        },
        "total": len(items),
        "items": items,
    }


# =====================================================================
# 阶段2：match
# =====================================================================
def lookup_with_tmdb(tmdb: TmdbClient, parsed: dict, bgm_client, bgm_always: bool) -> Optional[Dict[str, Any]]:
    """TMDb 主匹配流程：电影/剧集两路尝试，剧集再取单集信息，疑似动漫补 Bangumi。"""
    candidates = list(unique(parsed["candidates"]))
    year = parsed.get("year")
    kinds = ["tv", "movie"] if parsed["type"] == "episode" else ["movie", "tv"]

    chosen = None
    for kind in kinds:
        best = tmdb.search_best(kind, candidates, year)
        if best:
            chosen = (kind, best[1], best[0])
            break
    if not chosen:
        return None

    kind, res, score = chosen
    if kind == "movie":
        d = tmdb.movie_detail(res["id"])
        if not d:
            return None
        info = tmdb.movie_info(d)
        tmdb.en_fallback(info, "movie", d["id"])
        info["tmdb_url"] = f"https://www.themoviedb.org/movie/{d['id']}"
    else:
        d = tmdb.tv_detail(res["id"])
        if not d:
            return None
        info = tmdb.tv_info(d)
        tmdb.en_fallback(info, "tv", d["id"])
        info["tmdb_url"] = f"https://www.themoviedb.org/tv/{d['id']}"
        s, e = parsed.get("season"), parsed.get("episode")
        if s is not None and e is not None:
            ed = tmdb.episode_detail(d["id"], s, e)
            if ed:
                info["episode"] = tmdb.episode_info(ed, s, e)

    info["match"] = "exact" if score >= 100 else ("fuzzy" if score >= 40 else "weak")
    info["match_score"] = round(score, 1)

    # 疑似动漫（TMDb 分类含"动画/Animation"）或用户强制 -> Bangumi 补充
    genres = " ".join(info.get("genres") or [])
    is_anime = ("动画" in genres) or ("Animation" in genres) or ("アニメ" in genres)
    if bgm_client and (bgm_always or is_anime):
        queries = [info.get("original_title"), info.get("title_zh"), info.get("title"), *candidates]
        try:
            # 电影等非动漫内容要求精确匹配，避免"标题包含"式误匹配
            b = bgm_client.best_match(list(unique(queries)), strict=(info.get("type") == "movie" and not is_anime))
        except BangumiError as err:
            b = None
            info["bgm_error"] = str(err)
        if b:
            info["bgm"] = b
    return info


def pipeline_match(scan_report: Dict[str, Any], tmdb, bgm_client, imdb,
                   bgm_always: bool, limit: Optional[int], verbose: bool = False) -> Dict[str, Any]:
    items = list(scan_report.get("items") or [])
    if limit:
        items = items[:limit]

    out_items: List[Dict[str, Any]] = []
    for it in items:
        new_it = dict(it)
        parsed = it.get("parsed") or {}
        # scan.json 可能由旧版代码（或另一台主机）生成——parsing 逻辑的修复
        # （part 候选、season 缺省、别名候选等）不会体现在存量 parsed 里。
        # match 时用当前解析器重算：guessit 只解析文件名字符串，不需要文件存在，
        # NAS 路径（/Volumes/...）在任意主机上都可重放。
        if it.get("file"):
            try:
                mi = it.get("media_info") or {}
                reparsed = parse_video(
                    Path(str(it["file"])),
                    folder_hint=it.get("folder_hint") or "",
                    embedded_title=mi.get("embedded_title") or "",
                )
                if reparsed.get("candidates") or reparsed.get("title"):
                    parsed = reparsed
            except Exception:
                pass  # 重解析失败时退回 scan 存量数据
        lookup: Optional[Dict[str, Any]] = None
        status = "unmatched"

        if tmdb is not None:
            try:
                lookup = lookup_with_tmdb(tmdb, parsed, bgm_client, bgm_always)
                if lookup and lookup.get("match_score") is not None \
                        and lookup["match_score"] < UNCERTAIN_BELOW:
                    status = "uncertain"  # 低置信度：apply 默认跳过，人工确认后再放行
                else:
                    status = "ok" if lookup else "not_found"
            except TmdbError as e:
                status = "error"
                lookup = {"error": f"TMDb: {e}"}
        elif bgm_client is not None and (bgm_always or parsed.get("has_cjk")):
            # 离线但开了 Bangumi：中文资源仍可拿到中文名（电影要求精确匹配防误配）
            try:
                b = bgm_client.best_match(list(unique(parsed.get("candidates") or [])),
                                          strict=(parsed.get("type") != "episode"))
            except BangumiError as e:
                status = "error"
                lookup = {"error": f"Bangumi: {e}"}
            else:
                if b:
                    air = str(b.get("air_date") or "")
                    lookup = {"bgm": b, "year": air[:4] or None}
                    status = "ok"

        if imdb is not None and lookup and lookup.get("imdb_id"):
            lookup["imdb"] = imdb.enrich(lookup["imdb_id"])

        new_it["parsed"] = parsed  # 写回重解析结果，保证 match JSON 与实际查找一致
        new_it["lookup"] = lookup
        new_it["status"] = status
        out_items.append(new_it)
        if verbose:
            lk = lookup or {}
            label = lk.get("title_zh") or (lk.get("bgm") or {}).get("name_cn") or lk.get("title") or "-"
            print(f"    match: {Path(it.get('file', '?')).name} -> {status} | {label}")

    matched_via = "tmdb" if tmdb is not None else ("bgm" if bgm_client is not None else "none")
    counts = Counter(it["status"] for it in out_items)
    base = {k: v for k, v in scan_report.items() if k not in ("stage", "generated_at", "total", "items")}
    base.update({
        "tool": f"video_scraper {__version__}",
        "stage": "match",
        "generated_at": _now(),
        "match_params": {"source": matched_via, "bgm_always": bool(bgm_always),
                         "use_imdb": bool(imdb), "limit": limit},
        "total": len(out_items),
        "counts": dict(counts),
        "items": out_items,
    })
    return base


# =====================================================================
# 阶段3：apply
# =====================================================================
def _map_item_path(item: Dict[str, Any], map_root: List[str], base_dir: Optional[str]) -> Path:
    """把 scan 时记录的路径映射到当前主机（跨主机场景）。"""
    rel = item.get("relpath") or Path(item.get("file") or "").name
    if base_dir:
        return Path(base_dir) / rel
    root = item.get("root") or ""
    for pair in map_root or []:
        if "=" in pair:
            old, new = pair.split("=", 1)
            if root.lower() == str(Path(old)).lower().rstrip("\\/"):
                return Path(new) / rel
    return Path(item.get("file") or rel)


def _rename_target(item: Dict[str, Any], map_root: List[str], base_dir: Optional[str],
                   movie_template: str, episode_template: str, rename_dirs: bool):
    """计算某条目的重命名目标，返回 (源路径, 目标路径)；信息不足时目标为 None。"""
    src = _map_item_path(item, map_root, base_dir)
    new_parts = plan_rename(item, movie_template, episode_template, rename_dirs)
    if not new_parts:
        return src, None
    rel = item.get("relpath") or src.name
    depth = len([p for p in rel.split("/") if p])
    anchor = src.parents[depth - 1] if depth >= 1 and len(src.parents) >= depth else src.parent
    return src, anchor.joinpath(*new_parts)


def _move_and_clean(src: Path, dst: Path, stop_at: Path) -> None:
    """移动文件到新路径；旧目录因此搬空时逐级删除。

    只清理 src 与 stop_at（映射后的根目录）之间的旧子树，不会动到根目录本身。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    parent = src.parent
    try:
        parent.relative_to(stop_at)
    except ValueError:
        return
    while parent != stop_at and stop_at in parent.parents:
        if not parent.exists() or not parent.is_dir():
            break
        try:
            next(parent.iterdir())
            break  # 目录非空，停
        except StopIteration:
            pass
        except OSError:
            break
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def pipeline_apply(match_report: Dict[str, Any], *, rename: bool = False, rename_dirs: bool = True,
                   movie_template: str = MOVIE_TEMPLATE, episode_template: str = EPISODE_TEMPLATE,
                   write_nfo: bool = False, download_poster: bool = False,
                   download_subtitles: bool = False, subtitle_langs: str = "zh-cn,en",
                   map_root: Optional[List[str]] = None, base_dir: Optional[str] = None,
                   execute: bool = False, os_client=None, verbose: bool = False) -> Dict[str, Any]:
    actions: List[Dict[str, Any]] = []

    def record(file: str, action: str, status: str, detail: str = "",
               **extra: Any) -> None:
        a = {"file": file, "action": action, "status": status, "detail": detail}
        a.update(extra)
        actions.append(a)
        if verbose:
            print(f"    {action:9} [{status}] {Path(file).name} {detail}")

    # ---- 撞名预检：同批多个文件被规划到同一目标路径，几乎必然是误匹配 ----
    # （如"教父 1/2 都改成 教父 (1972)"）。第一个照常执行，其余整条跳过并标记
    # conflict，避免 execute 时"第一个成功、其余静默 skip"把问题埋进报告。
    conflict_files: set = set()
    if rename:
        seen_dst: Dict[str, str] = {}
        for it in match_report.get("items") or []:
            lk0 = it.get("lookup") or {}
            if it.get("status") != "ok" or not lk0 or lk0.get("error"):
                continue
            s, d = _rename_target(it, map_root or [], base_dir,
                                  movie_template, episode_template, rename_dirs)
            if d is None:
                continue
            k = str(d).lower()
            if k in seen_dst:
                conflict_files.add(str(s))
            else:
                seen_dst[k] = str(s)

    for item in match_report.get("items") or []:
        lk = item.get("lookup") or {}
        if item.get("status") == "uncertain":
            record(str(item.get("file") or "?"), "none", "skipped",
                   "匹配置信度低（score<50），默认跳过；确认无误可修改 match.json 后重跑")
            continue
        if item.get("status") != "ok" or not lk or lk.get("error"):
            continue
        src = _map_item_path(item, map_root or [], base_dir)
        if str(src) in conflict_files:
            record(str(src), "rename", "conflict",
                   "同批多个文件被规划到同一目标路径（可能存在误匹配），整条跳过")
            continue
        if not src.exists():
            record(str(src), "none", "error", "文件不存在（跨主机？请用 --map-root / --base-dir 映射路径）")
            continue

        # ---- 重命名 ----
        final_path = src
        if rename:
            _, dst = _rename_target(item, map_root or [], base_dir,
                                    movie_template, episode_template, rename_dirs)
            if dst is None:
                record(str(src), "rename", "skipped", "信息不足以确定新名字")
            elif dst == src:
                record(str(src), "rename", "skipped", "已是目标名")
            elif dst.exists():
                record(str(src), "rename", "skipped", f"目标已存在: {dst.name}")
            elif execute:
                try:
                    _move_and_clean(src, dst, dst.parent)
                    record(str(src), "rename", "done", str(dst), to=str(dst))
                    final_path = dst
                except OSError as e:
                    record(str(src), "rename", "error", str(e))
            else:
                record(str(src), "rename", "planned", str(dst), to=str(dst))
                final_path = dst  # dry-run 时后续动作按计划路径展示

        # ---- NFO ----
        if write_nfo and lk.get("tmdb_id"):
            nfo_target = final_path.with_suffix(".nfo")
            if nfo_target.exists():
                record(str(nfo_target), "nfo", "skipped", "已存在")
            elif execute:
                nfo = write_nfo_for(final_path, {"lookup": lk})
                record(str(final_path), "nfo", "done" if nfo else "error", str(nfo or "写入失败"))
            else:
                record(str(nfo_target), "nfo", "planned")

        # ---- 海报 ----
        if download_poster:
            bgm = lk.get("bgm") or {}
            url = lk.get("poster_w500") or lk.get("poster") or bgm.get("cover")
            if not url:
                record(str(final_path), "poster", "skipped", "无海报地址")
            else:
                target = final_path.parent / "poster.jpg"
                if target.exists():
                    record(str(target), "poster", "skipped", "已存在")
                elif execute:
                    try:
                        import requests

                        r = requests.get(url, timeout=30)
                        r.raise_for_status()
                        target.write_bytes(r.content)
                        record(str(target), "poster", "done", url)
                    except Exception as e:
                        record(str(target), "poster", "error", str(e))
                else:
                    record(str(target), "poster", "planned", url)

        # ---- 字幕 ----
        if download_subtitles:
            imdb_id = lk.get("imdb_id")
            srt_target = final_path.with_name(final_path.stem + ".zh.srt")
            if not imdb_id:
                record(str(srt_target), "subtitle", "skipped", "缺少 IMDb ID")
            elif srt_target.exists():
                record(str(srt_target), "subtitle", "skipped", "已存在")
            elif os_client is None:
                record(str(srt_target), "subtitle", "skipped", "未配置 OpenSubtitles（--os-key / OPENSUBTITLES_API_KEY）")
            elif execute:
                try:
                    langs = [x.strip() for x in subtitle_langs.split(",") if x.strip()]
                    content, meta = os_client.download_srt(imdb_id, langs)
                    if not content:
                        record(str(srt_target), "subtitle", "skipped", meta.get("reason", "无字幕"))
                    else:
                        srt_target.write_bytes(content)
                        record(str(srt_target), "subtitle", "done",
                               f"lang={meta.get('language')} release={meta.get('release')}")
                except Exception as e:
                    record(str(srt_target), "subtitle", "error", str(e))
            else:
                record(str(srt_target), "subtitle", "planned", f"imdb:{imdb_id}")

    counts = Counter(a["status"] for a in actions)
    return {
        "tool": f"video_scraper {__version__}",
        "stage": "apply",
        "generated_at": _now(),
        "dry_run": not execute,
        "apply_params": {
            "rename": rename, "rename_dirs": rename_dirs, "write_nfo": write_nfo,
            "download_poster": download_poster, "download_subtitles": download_subtitles,
            "subtitle_langs": subtitle_langs, "map_root": list(map_root or []),
            "base_dir": base_dir,
        },
        "counts": dict(counts),
        "actions": actions,
    }
