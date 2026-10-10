"""文件名/文件夹名解析：guessit 为主，中文与发布组风格命名提供清洗兜底。

产出统一的解析结果：
- type: movie / episode
- title / year / season / episode
- candidates: 供在线搜索用的候选标题列表（按优先级排序）
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from guessit import guessit

# CJK 字符（含假名/谚文）——用于判断是否疑似动漫/中文资源
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
# 连续汉字段（候选标题兜底）
CJK_RUN_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{2,}(?:[\s·][\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]{2,})*"
)
YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")
# 各种括号段：[发布组] (2019) 【简繁】 等
BRACKET_RE = re.compile(r"[\[\(【（｛{]([^\]\)】）｝}]{1,60})[\]\)】）｝}]")
LEADING_BRACKET_RE = re.compile(r"^(?:\s*[\[\(【（｛{][^\]\)】）｝}]{1,60}[\]\)】）｝}])+")

# 发布组/压制参数等噪声词（仅在 guessit 失效时的兜底清洗用）
NOISE_TOKEN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:"
    r"1080[pi]|720p|2160p|4320p|4k|8k|uhd|sdr|hdr10\+|hdr|dv|dovi|dolby|vision|atmos|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|av1|divx|xvid|"
    r"blu-?ray|bdrip|bdmv|remux|web-?dl|webrip|hdtv|pdtv|dvdrip|dvd|"
    r"truehd|dts(?:-?hd)?(?:[. ]?ma)?|ac-?3|eac-?3|ddp|aac|flac|opus|mp3|"
    r"10bit|8bit|hi10p|60fps|24fps|"
    r"chs|cht|big5|gbk|mandarin|cantonese|"
    r"complete|extended|remastered|unrated|proper|repack|limited|internal|"
    r"subbed|dualaudio|dual"
    r")(?![A-Za-z0-9])"
)

# 明显不是片名的中文段
CJK_NOISE_HINTS = ("字幕组", "汉化组", "汉化", "简体", "繁体", "内嵌", "双语", "国粤", "简繁", "熟肉")

# 剧集文件候选标题末尾的季/集号（例如 "Some Anime 01" / "Show S01"）
SEASON_EP_TRAILING_RE = re.compile(
    r"(?:[\s._-]*(?:[Ss]\d{1,2})?[\s._-]*(?:[Ee][Pp]?)?\d{1,4}"
    r"|[\s._-]+[Ss]\d{1,2}"
    r"|[\s._-]+[Ss]eason\s*\d{1,2}"
    r"|第\s*\d+\s*[集话話期])$"
)


def _plain(v) -> Optional[str]:
    """guessit 的值可能是嵌套结构，统一转成可读字符串。"""
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return ", ".join(str(_plain(x)) for x in v)
    return str(v)


def _as_int(v) -> Optional[int]:
    if v is None:
        return None
    m = re.search(r"\d+", str(v))
    return int(m.group()) if m else None


def _roman(n: int) -> str:
    """整数转罗马数字（续作部数用，够覆盖常见续作）。"""
    vals = [(10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = []
    for v, s in vals:
        while n >= v:
            out.append(s)
            n -= v
    return "".join(out) or str(n)


def _strip_trailing_year(c: str) -> str:
    m = re.search(r"[\s._-]*[\(\[]?((?:19|20)\d{2})[\)\]]?\s*$", c)
    # 整名就是年份的片名（如《2012》）不能清掉
    if m and len(c) - len(m.group(0)) >= 2:
        return c[: m.start()].strip(" -_.·")
    return c


def manual_clean(s: Optional[str]) -> str:
    """去掉括号段和噪声词，把分隔符归一为空格。"""
    if not s:
        return ""
    s = BRACKET_RE.sub(" ", s)
    s = NOISE_TOKEN_RE.sub(" ", s)
    s = re.sub(r"[-_.·]+", " ", s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip(" -_.·~")


def manual_clean_keep_group(s: Optional[str]) -> str:
    """只去掉开头连续的括号段（保留中段可能存在的中文片名），再走常规清洗。"""
    if not s:
        return ""
    return manual_clean(LEADING_BRACKET_RE.sub(" ", s))


def _cjk_candidate(s: str) -> Optional[str]:
    """从字符串里挑出最长的汉字段作为候选标题（跳过字幕组等噪声）。"""
    best = ""
    for m in CJK_RUN_RE.finditer(s or ""):
        run = m.group().strip()
        if not run or run in CJK_NOISE_HINTS:
            continue
        if any(h in run for h in CJK_NOISE_HINTS):
            continue
        if len(run) > len(best):
            best = run
    return best or None


def parse_video(path: Path, folder_hint: str = "", embedded_title: str = "") -> dict:
    """解析单个视频文件。

    :param path: 视频文件路径
    :param folder_hint: 对识别有帮助的目录名（见 scanner._folder_hint）
    :param embedded_title: 视频内部元数据里的 title 标签（优先级最高的候选）
    """
    g = guessit(path.name)
    raw = {k: _plain(v) for k, v in g.items()}
    gtype = raw.get("type") or ("episode" if raw.get("episode") else "movie")
    stem = path.stem

    # 年份：guessit > 目录名 > 内嵌标题 > 文件名
    year = None
    for src in (raw.get("year"), folder_hint, embedded_title, stem):
        if not src:
            continue
        m = YEAR_RE.search(str(src))
        if m:
            year = int(m.group(1))
            break

    cands: List[str] = []

    def add(c) -> None:
        if not c:
            return
        c = str(c).strip(" -_.·~")
        if len(c) > 4:
            c = _strip_trailing_year(c)
        if c and c.lower() not in {x.lower() for x in cands}:
            cands.append(c)

    add(embedded_title)
    # 续作部数：guessit 把 "Part II" 拆进 part 字段；拼回部数的候选必须排在
    # guessit title 之前——后者丢了部数，却常能"精确"命中系列第一部
    # （如教父2 被锁到 1972 的教父1）。
    part_num = _as_int(g.get("part"))
    if part_num and raw.get("title"):
        add(f"{raw['title']} Part {_roman(part_num)}")
        add(f"{raw['title']} Part {part_num}")
    add(raw.get("title"))
    # 其他语言/别名标题（guessit 可能产出 list 或单值，如 "英雄联盟：双城之战"）
    alt = g.get("alternative_title")
    if isinstance(alt, (list, tuple)):
        for a in alt:
            add(_plain(a))
    elif alt:
        add(_plain(alt))
    add(folder_hint)
    add(manual_clean(stem))
    add(manual_clean_keep_group(stem))

    if gtype == "episode":
        # 去掉尾部集号再给一个候选（如 "Some Anime 01" -> "Some Anime"）
        for c in list(cands):
            m = SEASON_EP_TRAILING_RE.search(c)
            if m and len(c[: m.start()].strip()) >= 2:
                add(c[: m.start()])

    add(_cjk_candidate(stem + " " + (folder_hint or "")))
    add(stem)  # 最后兜底：原始文件名（TMDb 模糊搜索对噪声容忍度尚可）

    # TMDb 对过长 query 容易失败
    cands = [c[:120] for c in cands]

    season = _as_int(raw.get("season"))
    episode = _as_int(raw.get("episode"))
    if gtype == "episode" and season is None and episode is not None:
        season = 1  # 文件名无季号的剧集默认 S01（行业惯例，动漫/老剧常见）

    return {
        "type": gtype,
        "title": raw.get("title"),
        "year": year,
        "season": season,
        "episode": episode,
        "episode_title": raw.get("episode_title"),
        "screen_size": raw.get("screen_size"),
        "video_codec": raw.get("video_codec"),
        "audio_codec": raw.get("audio_codec"),
        "release_group": raw.get("release_group"),
        "candidates": cands,
        "has_cjk": bool(CJK_RE.search(stem + (folder_hint or ""))),
        "guessit": raw,
    }
