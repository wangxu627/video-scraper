"""结果输出：JSON 报告 / CSV 摘要 / Kodi 兼容 .nfo / 海报下载。"""
from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

CSV_FIELDS = [
    "file", "status", "type", "title", "title_zh", "original_title", "year",
    "rating", "runtime_min", "tmdb_id", "imdb_id", "episode", "bgm_name_cn",
    "tmdb_url", "bgm_url", "resolution", "duration_min",
]


def write_json_report(report: Dict[str, Any], path: Path) -> None:
    Path(path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def flatten(item: Dict[str, Any]) -> Dict[str, Any]:
    """把单条记录压平成 CSV 行。"""
    lk = item.get("lookup") or {}
    ep = lk.get("episode") or {}
    bgm = lk.get("bgm") or {}
    ep_str = ""
    if ep.get("season") is not None and ep.get("episode") is not None:
        name = ep.get("name") or ""
        ep_str = f"S{ep['season']:02d}E{ep['episode']:02d} {name}".strip()
    return {
        "file": item.get("file"),
        "status": item.get("status"),
        "type": lk.get("type"),
        "title": lk.get("title"),
        "title_zh": lk.get("title_zh"),
        "original_title": lk.get("original_title"),
        "year": lk.get("year"),
        "rating": lk.get("rating"),
        "runtime_min": lk.get("runtime_min"),
        "tmdb_id": lk.get("tmdb_id"),
        "imdb_id": lk.get("imdb_id"),
        "episode": ep_str,
        "bgm_name_cn": bgm.get("name_cn"),
        "tmdb_url": lk.get("tmdb_url"),
        "bgm_url": bgm.get("url"),
    }


def write_csv_summary(items: List[Dict[str, Any]], path: Path) -> None:
    # utf-8-sig：让 Excel 直接打开不乱码
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for it in items:
            row = flatten(it)
            mi = it.get("media_info") or {}
            if mi.get("width") and mi.get("height"):
                row["resolution"] = f"{mi['width']}x{mi['height']}"
            if mi.get("duration_sec"):
                row["duration_min"] = round(mi["duration_sec"] / 60)
            w.writerow(row)


# ---------- Kodi 兼容 .nfo ----------
def _sub(parent: ET.Element, tag: str, text) -> None:
    if text is None or text == "":
        return
    ET.SubElement(parent, tag).text = str(text)


def movie_nfo(lk: Dict[str, Any]) -> ET.Element:
    root = ET.Element("movie")
    _sub(root, "title", lk.get("title_zh") or lk.get("title"))
    _sub(root, "originaltitle", lk.get("original_title"))
    _sub(root, "rating", lk.get("rating"))
    _sub(root, "votes", lk.get("votes"))
    _sub(root, "year", lk.get("year"))
    _sub(root, "premiered", lk.get("release_date"))
    _sub(root, "plot", lk.get("overview"))
    _sub(root, "runtime", lk.get("runtime_min"))
    for g in lk.get("genres") or []:
        _sub(root, "genre", g)
    if lk.get("imdb_id"):
        u = ET.SubElement(root, "uniqueid", {"type": "imdb", "default": "true"})
        u.text = lk["imdb_id"]
        _sub(root, "imdbid", lk.get("imdb_id"))
    if lk.get("tmdb_id") is not None:
        u = ET.SubElement(root, "uniqueid", {"type": "tmdb"})
        u.text = str(lk["tmdb_id"])
        _sub(root, "tmdbid", str(lk["tmdb_id"]))
    if lk.get("poster"):
        t = ET.SubElement(root, "thumb", {"aspect": "poster"})
        t.text = lk["poster"]
    if lk.get("backdrop"):
        fa = ET.SubElement(root, "fanart")
        t = ET.SubElement(fa, "thumb")
        t.text = lk["backdrop"]
    return root


def episode_nfo(lk: Dict[str, Any]) -> ET.Element:
    ep = lk.get("episode") or {}
    root = ET.Element("episodedetails")
    _sub(root, "title", ep.get("name") or lk.get("title_zh") or lk.get("title"))
    _sub(root, "showtitle", lk.get("title_zh") or lk.get("title"))
    _sub(root, "season", ep.get("season"))
    _sub(root, "episode", ep.get("episode"))
    _sub(root, "aired", ep.get("air_date"))
    _sub(root, "plot", ep.get("overview"))
    _sub(root, "rating", ep.get("rating"))
    if ep.get("tmdb_id"):
        u = ET.SubElement(root, "uniqueid", {"type": "tmdb", "default": "true"})
        u.text = str(ep["tmdb_id"])
    if lk.get("tmdb_id") is not None:
        u = ET.SubElement(root, "uniqueid", {"type": "tmdbshow"})
        u.text = str(lk["tmdb_id"])
    if ep.get("still"):
        t = ET.SubElement(root, "thumb")
        t.text = ep["still"]
    return root


def write_nfo_for(video_path: Path, item: Dict[str, Any]) -> Optional[Path]:
    lk = item.get("lookup") or {}
    if not lk.get("tmdb_id"):
        return None
    if lk.get("type") == "tv" and not lk.get("episode"):
        return None  # 只匹配到剧集本身、无单集信息时不写（避免生成错误的 movie nfo）
    try:
        root = episode_nfo(lk) if lk.get("type") == "tv" else movie_nfo(lk)
        tree = ET.ElementTree(root)
        if hasattr(ET, "indent"):
            ET.indent(tree, space="  ")
        nfo_path = Path(video_path).with_suffix(".nfo")
        tree.write(nfo_path, encoding="utf-8", xml_declaration=True)
        return nfo_path
    except Exception as e:
        print(f"[警告] NFO 写入失败 {Path(video_path).name}: {e}")
        return None


def download_poster_for(video_path: Path, item: Dict[str, Any]) -> Optional[Path]:
    """下载海报到视频所在目录（poster.jpg），已存在则跳过。"""
    lk = item.get("lookup") or {}
    url = lk.get("poster_w500") or lk.get("poster")
    if not url:
        return None
    target = Path(video_path).parent / "poster.jpg"
    if target.exists():
        return None
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        target.write_bytes(r.content)
        return target
    except Exception as e:
        print(f"[警告] 海报下载失败 {Path(video_path).name}: {e}")
        return None
