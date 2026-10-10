"""阶段3的重命名规划：根据匹配结果生成新的文件名/目录名（不直接动文件系统）。

命名规则（默认，可用 --movie-template / --episode-template 覆盖）：
- 电影：   `盗梦空间 (2010).mkv`；文件若在专属子目录里，目录一并改成同名
- 剧集：   `进击的巨人 (2013)/Season 01/进击的巨人 - S01E01 - 集名.mkv`
           （Season 目录统一规范成 `Season 01` 两位数格式）
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .scanner import SEASON_DIR_RE

ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

MOVIE_TEMPLATE = "{title} ({year})"
EPISODE_TEMPLATE = "{title} - S{ss}E{ee}{ep_suffix}"


def safe_name(s: Optional[str], fallback: str = "Unknown") -> str:
    """清理 Windows/Linux 文件名非法字符。"""
    out = ILLEGAL_RE.sub(" ", str(s or "")).strip(" .")
    return out or fallback


def _render(template: str, **fields: Any) -> str:
    """渲染模板并收尾：去掉空括号 ()、多余空格。"""
    out = template.format(**fields)
    out = re.sub(r"\(\s*\)", "", out)          # 年份缺失时不留 "()"
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip(" -_.·~")


def _lookup_year(lk: Dict[str, Any]) -> str:
    bgm_air = str((lk.get("bgm") or {}).get("air_date") or "")
    return str(lk.get("year") or (bgm_air[:4] if bgm_air else "") or "")


def _lookup_type(lk: Dict[str, Any], parsed: Dict[str, Any]) -> str:
    """TMDb 结果自带 type；bgm-only 结果用 guessit 的类型推断。"""
    return lk.get("type") or ("tv" if parsed.get("type") == "episode" else "movie")


def display_title(lk: Dict[str, Any]) -> Optional[str]:
    """展示名优先级：中文名 > 本地标题 > Bangumi 中文名 > 原始标题。"""
    for key in ("title_zh", "title"):
        if lk.get(key):
            return lk[key]
    bgm = lk.get("bgm") or {}
    if bgm.get("name_cn") or bgm.get("name"):
        return bgm.get("name_cn") or bgm.get("name")
    return lk.get("original_title")


def plan_rename(
    item: Dict[str, Any],
    movie_template: str = MOVIE_TEMPLATE,
    episode_template: str = EPISODE_TEMPLATE,
    rename_dirs: bool = True,
) -> Optional[List[str]]:
    """计算该条目的新 relpath（相对其根目录的 posix 路径）；无需改名时返回 None。"""
    lk = item.get("lookup") or {}
    parsed = item.get("parsed") or {}
    rel = item.get("relpath") or Path(item.get("file") or "").name
    parts = [p for p in rel.split("/") if p]
    if not parts:
        return None
    ext = Path(parts[-1]).suffix or ""
    title = display_title(lk) or parsed.get("title")
    if not title:
        return None
    year = _lookup_year(lk)
    lk_type = _lookup_type(lk, parsed)

    if lk_type == "tv":
        s, e = parsed.get("season"), parsed.get("episode")
        if s is None or e is None:
            return None  # 没有季集号，保守不改名
        ep = (lk.get("episode") or {}).get("name")
        ep_suffix = f" - {safe_name(ep, '')}" if ep else ""
        new_name = _render(
            episode_template,
            title=safe_name(title), show=safe_name(title), year=year,
            ss=f"{int(s):02d}", ee=f"{int(e):02d}",
            ep=safe_name(ep, ""), ep_suffix=ep_suffix,
        ) + ext
        heads = parts[:-1]
        season_dir_name = f"Season {int(s):02d}"
        if not heads:
            # 文件直接躺在根目录：需要新建 剧名/Season 01 目录（仅 rename_dirs 时）
            return ([safe_name(title), season_dir_name, new_name] if rename_dirs
                    else [new_name])
        # 特例：根目录下直接是 Season 目录（没有剧名层）
        if len(heads) == 1 and SEASON_DIR_RE.match(heads[0].strip()):
            return ([safe_name(title), season_dir_name, new_name] if rename_dirs
                    else [heads[0], new_name])
        new_heads: List[str] = []
        season_idx = None
        for i, h in enumerate(heads):
            if SEASON_DIR_RE.match(h.strip()):
                season_idx = i
                break
        for i, h in enumerate(heads):
            if i == 0 and rename_dirs:
                show_name = f"{safe_name(title)} ({year})" if year else safe_name(title)
                new_heads.append(show_name)
            elif season_idx is not None and i == season_idx:
                new_heads.append(season_dir_name)
            else:
                new_heads.append(h)  # 其他中间层保持不变
        return new_heads + [new_name]

    # 电影
    original = safe_name(lk.get("original_title"), "")
    new_name = _render(movie_template, title=safe_name(title), year=year,
                       original_title=original) + ext
    heads = parts[:-1]
    if heads and rename_dirs:
        dir_name = _render(movie_template, title=safe_name(title), year=year,
                           original_title=original)
        return [safe_name(dir_name)] + heads[1:] + [new_name]
    return heads + [new_name]
