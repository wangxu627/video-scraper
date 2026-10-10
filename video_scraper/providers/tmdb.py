"""TMDb 客户端：搜索 + 详情，取中文标题/简介，并映射 IMDb ID。

说明：
- TMDb 官方支持 zh-CN 本地化数据，是"中文名"的主要来源（Jellyfin/Emby 同款做法）；
- 通过 external_ids 拿到对应的 IMDb ID；通过 translations 端点提取 zh-CN/zh-TW/zh-HK 标题；
- v3 api_key（32 位十六进制）与 v4 Read Access Token（eyJ 开头）都支持。
"""
from __future__ import annotations

import re
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

import requests

BASE = "https://api.themoviedb.org/3"
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
ZH_PRIORITIES = ("zh-CN", "zh-TW", "zh-HK", "zh")


class TmdbError(Exception):
    pass


def _norm(s) -> str:
    # NFKD 再过滤：既折叠拉丁扩展字母（TMDb 官方名 "Shōgun" 的 ō -> o），
    # 也让 macOS NFD 文件名（浊音假名 = ウ+浊点两码点）与 TMDb 的 NFC（単码点 ヴ）对齐——
    # 正则保留范围恰好包含假名浊点 combining mark，日文双向折叠后一致。
    s = unicodedata.normalize("NFKD", str(s or ""))
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]+", "", s.lower())


def _result_year(res: Dict[str, Any]) -> Optional[int]:
    """从搜索/详情结果里取发行年份。"""
    for k in ("release_date", "first_air_date"):
        if res.get(k):
            return int(str(res[k])[:4])
    return None


def score_result(res: Dict[str, Any], query: str, year: Optional[int]) -> float:
    """给搜索结果打分：标题精确匹配 > 模糊包含 > 年份接近 > 热度。"""
    q = _norm(query)
    names = {_norm(res.get(k)) for k in ("title", "original_title", "name", "original_name")}
    names.discard("")
    s = 0.0
    if q:
        if q in names:
            s += 100.0
        elif any(q in n or n in q for n in names):
            s += 40.0
    ry = _result_year(res)
    if year and ry:
        s += max(0.0, 20.0 - abs(year - ry) * 4)
    s += min(float(res.get("popularity") or 0.0), 10.0)
    return s


def zh_from_translations(translations: Dict[str, Any], field: str = "title") -> Optional[str]:
    """从 translations 数据里按 zh-CN > zh-TW > zh-HK > zh 优先级取中文名。"""
    found: Dict[str, str] = {}
    for t in (translations or {}).get("translations") or []:
        lang = t.get("iso_639_1") or ""
        region = t.get("iso_3166_1") or ""
        data = t.get("data") or {}
        val = data.get(field) or data.get("name") or data.get("title")
        if val:
            found.setdefault(f"{lang}-{region}" if region else lang, val)
    for key in ZH_PRIORITIES:
        if found.get(key):
            return found[key]
    return None


class TmdbClient:
    def __init__(self, api_key: str, language: str = "zh-CN", cache=None, delay: float = 0.25):
        if not api_key:
            raise TmdbError("缺少 TMDb API Key")
        self.language = language
        self.cache = cache
        self.delay = delay
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"
        if api_key.startswith("eyJ"):
            # v4 Read Access Token
            self.session.headers["Authorization"] = f"Bearer {api_key}"
            self._auth_params: Dict[str, str] = {}
        else:
            # v3 api key
            self._auth_params = {"api_key": api_key}

    # ---------- 底层请求 ----------
    def _get(self, path: str, **params) -> Optional[Dict[str, Any]]:
        cache_key = "tmdb:" + path + ":" + repr(sorted(params.items()))
        if self.cache is not None:
            hit = self.cache.get(cache_key)
            if hit is not None:
                return hit

        params = {k: v for k, v in params.items() if v is not None}
        params.update(self._auth_params)
        last_err = ""
        for _ in range(4):
            try:
                r = self.session.get(BASE + path, params=params, timeout=20)
            except requests.RequestException as e:
                last_err = f"网络错误: {e}"
                time.sleep(1.0)
                continue
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After") or 2) + 1
                time.sleep(wait)
                continue
            if r.status_code == 401:
                raise TmdbError("认证失败(401)：API Key 无效或未生效（新 key 需等几分钟生效）")
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                raise TmdbError(f"HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            if self.cache is not None:
                self.cache.set(cache_key, data)
            time.sleep(self.delay)
            return data
        raise TmdbError(last_err or "请求多次重试后仍失败（429 限流）")

    # ---------- 搜索 ----------
    def _search(self, kind: str, query: str, year: Optional[int], with_year: bool,
                language: str = "en-US") -> Dict[str, Any]:
        # TMDb 搜索引擎按 NFC 码点匹配：macOS NFD 文件名里的浊音假名
        # （ウ+浊点两码点）搜索恒 0 结果，NFC（単码点 ヴ）则正常命中
        # （实测：NFD "新世紀エヴァンゲリオン" zh-CN/en-US 均 0 结果，
        #   NFC 得 2 条含 tv/890）。统一 NFC 化后再发请求；
        # 评分侧 _norm 是 NFKD，NFC/NFD 归一后一致，不受影响。
        query = unicodedata.normalize("NFC", str(query or ""))
        # 评分用 en-US：zh-CN 本地化名会让英文原题拿不到标题分
        # （如 "Slam Duck"——真灌篮高手的 zh 名是"灌篮高手"，韩综名反而含英文）。
        # 电影用 year 硬过滤；剧集不用——续季文件名里的年份是播出年，不是首播年，
        # 硬过滤会把真条目（如 Arcane 首播 2021）整个滤掉。
        params: Dict[str, Any] = {
            "query": query,
            "language": language,
            "include_adult": "false",
            "page": 1,
        }
        if with_year and year and kind == "movie":
            params["year"] = year
        return self._get(f"/search/{kind}", **params) or {}

    def search_best(
        self, kind: str, candidates: List[str], year: Optional[int] = None
    ) -> Optional[Tuple[float, Dict[str, Any]]]:
        """按候选标题逐个搜索，返回 (最高分, 最佳结果)；精确命中即提前返回。"""
        best: Optional[Tuple[float, Dict[str, Any]]] = None
        for q in candidates:
            if not q or re.fullmatch(r"[\d\s._-]+", q):
                continue  # 纯数字/分隔符候选无信息量（如 "4.mkv"），跳过防垃圾命中
            # 按候选语言选搜索语言：TMDb 返回的本地化名要和 query 同语言才对得上分
            # （中文 query 配 zh-CN 拿中文名，英文 query 配 en-US 拿英文名；
            #   首选语言零结果才退到另一个）。电影才尝试带年份的变体；剧集恒不带
            # （见 _search 注释）。
            first = "zh-CN" if CJK_RE.search(q) else "en-US"
            lang_order = [first] + [l for l in ("en-US", self.language) if l != first]
            for with_year in (([True, False] if year else [False]) if kind == "movie" else [False]):
                results: List[Dict[str, Any]] = []
                for lang in lang_order:
                    data = self._search(kind, q, year, with_year, language=lang)
                    results = data.get("results") or []
                    if results:
                        break
                for r in results:
                    sc = score_result(r, q, year)
                    if best is None or sc > best[0]:
                        best = (sc, r)
                if best and best[0] >= 100:
                    # 精确命中但年份差 >=2：可能是同名系列另一部（如 "The Godfather"
                    # 精确命中 1972 而文件是 1974 的 Part II），继续尝试后续候选
                    ry = _result_year(best[1])
                    if not (year and ry and abs(year - ry) > 1):
                        return best
                if results:
                    break  # 该候选已有结果，不再尝试去掉年份的变体
        return best

    # ---------- 详情 ----------
    def movie_detail(self, mid, language: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return self._get(
            f"/movie/{mid}",
            language=language or self.language,
            append_to_response="external_ids,translations",
        )

    def tv_detail(self, tid, language: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return self._get(
            f"/tv/{tid}",
            language=language or self.language,
            append_to_response="external_ids,translations",
        )

    def episode_detail(self, tid, season: int, episode: int) -> Optional[Dict[str, Any]]:
        return self._get(f"/tv/{tid}/season/{season}/episode/{episode}", language=self.language)

    # ---------- 信息整形 ----------
    def movie_info(self, d: Dict[str, Any]) -> Dict[str, Any]:
        title_local = d.get("title") or ""
        zh = zh_from_translations(d.get("translations") or {}, "title")
        poster = d.get("poster_path")
        backdrop = d.get("backdrop_path")
        return {
            "type": "movie",
            "tmdb_id": d.get("id"),
            "imdb_id": (d.get("external_ids") or {}).get("imdb_id"),
            "title": title_local or None,
            "original_title": d.get("original_title"),
            "title_zh": zh or (title_local if CJK_RE.search(title_local) else None),
            "year": (d.get("release_date") or "")[:4] or None,
            "release_date": d.get("release_date") or None,
            "rating": d.get("vote_average"),
            "votes": d.get("vote_count"),
            "overview": d.get("overview") or None,
            "genres": [g.get("name") for g in d.get("genres") or [] if g.get("name")],
            "runtime_min": d.get("runtime"),
            "poster": f"https://image.tmdb.org/t/p/original{poster}" if poster else None,
            "poster_w500": f"https://image.tmdb.org/t/p/w500{poster}" if poster else None,
            "backdrop": f"https://image.tmdb.org/t/p/original{backdrop}" if backdrop else None,
        }

    def tv_info(self, d: Dict[str, Any]) -> Dict[str, Any]:
        name_local = d.get("name") or ""
        zh = zh_from_translations(d.get("translations") or {}, "name")
        poster = d.get("poster_path")
        backdrop = d.get("backdrop_path")
        erts = d.get("episode_run_time") or []
        return {
            "type": "tv",
            "tmdb_id": d.get("id"),
            "imdb_id": (d.get("external_ids") or {}).get("imdb_id"),
            "title": name_local or None,
            "original_title": d.get("original_name"),
            "title_zh": zh or (name_local if CJK_RE.search(name_local) else None),
            "year": (d.get("first_air_date") or "")[:4] or None,
            "first_air_date": d.get("first_air_date") or None,
            "rating": d.get("vote_average"),
            "votes": d.get("vote_count"),
            "overview": d.get("overview") or None,
            "genres": [g.get("name") for g in d.get("genres") or [] if g.get("name")],
            "seasons_total": d.get("number_of_seasons"),
            "episodes_total": d.get("number_of_episodes"),
            "runtime_min": max(erts) if erts else None,
            "poster": f"https://image.tmdb.org/t/p/original{poster}" if poster else None,
            "poster_w500": f"https://image.tmdb.org/t/p/w500{poster}" if poster else None,
            "backdrop": f"https://image.tmdb.org/t/p/original{backdrop}" if backdrop else None,
        }

    def episode_info(self, ed: Dict[str, Any], season: int, episode: int) -> Dict[str, Any]:
        still = ed.get("still_path")
        return {
            "season": season,
            "episode": episode,
            "name": ed.get("name"),
            "air_date": ed.get("air_date"),
            "rating": ed.get("vote_average"),
            "overview": ed.get("overview") or None,
            "still": f"https://image.tmdb.org/t/p/original{still}" if still else None,
            "tmdb_id": ed.get("id"),
        }

    def en_fallback(self, info: Dict[str, Any], kind: str, mid) -> Dict[str, Any]:
        """中文标题/简介缺失时，用 en-US 数据补 title_en / overview（兜底）。"""
        if not (self.language or "").lower().startswith("zh"):
            return info
        if info.get("title_zh") and info.get("overview"):
            return info
        d = self._get(f"/{kind}/{mid}", language="en-US")
        if not d:
            return info
        if not info.get("title_en"):
            info["title_en"] = d.get("title") or d.get("name")
        if not info.get("original_title") and d.get("original_title"):
            info["original_title"] = d.get("original_title")
        if not info.get("overview"):
            info["overview"] = d.get("overview")
        return info
