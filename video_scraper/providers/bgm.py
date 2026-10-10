"""Bangumi (bgm.tv) 客户端：动漫条目的中文名(name_cn)/简介/评分。

Bangumi 的 API 是开放的，不需要 key，但要求设置有意义的 User-Agent。
搜索端点实测（2026-09）：v0 的搜索接口（POST /v0/post/p1/subjects、
GET /v0/search/subjects）在线上返回 404 未部署；实际可用的是经典接口
GET /search/subject/{关键词}?type=2（响应 {"results": n, "list": [...]}），
条目详情用 GET /v0/subjects/{id}（v0 正常可用）。
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

BASE = "https://api.bgm.tv"
UA = "video-scraper/0.1 (local media library tool)"


class BangumiError(Exception):
    pass


def _norm(s) -> str:
    return re.sub(r"[\s·・:：~～'’.]+", "", str(s or "").lower())


class BangumiClient:
    def __init__(self, cache=None, delay: float = 0.3):
        self.cache = cache
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept": "application/json"})

    # ---------- 底层请求 ----------
    def _request(self, method: str, path: str, **kwargs):
        last = ""
        for _ in range(3):
            try:
                r = self.session.request(method, BASE + path, timeout=20, **kwargs)
            except requests.RequestException as e:
                last = f"网络错误: {e}"
                time.sleep(1.0)
                continue
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After") or 2) + 1)
                continue
            if r.status_code >= 400:
                raise BangumiError(f"HTTP {r.status_code}: {r.text[:200]}")
            time.sleep(self.delay)
            return r.json() if (r.content or b"").strip() else {}
        raise BangumiError(last or "请求多次重试后仍失败")

    # ---------- 搜索 ----------
    def search(self, keyword: str) -> List[Dict[str, Any]]:
        key = f"bgm:search:{keyword}"
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                return hit

        q = quote(keyword, safe="")
        items: List[Dict[str, Any]] = []
        # 先限定动画条目（type=2），没有结果再放宽到全部类型
        for params in ({"type": 2}, {}):
            data = self._request("GET", f"/search/subject/{q}", params=params)
            items = data.get("list") or data.get("data") or data.get("result") or []
            if items:
                break

        if self.cache is not None:
            self.cache.set(key, items)
        return items

    def subject(self, subject_id) -> Optional[Dict[str, Any]]:
        try:
            return self._request("GET", f"/v0/subjects/{subject_id}")
        except BangumiError:
            return None

    # ---------- 匹配 ----------
    def best_match(self, queries: List[str], strict: bool = False) -> Optional[Dict[str, Any]]:
        """按候选词逐个搜索，返回最佳条目。

        strict=True（电影等非动漫内容）：只接受标题精确匹配，宁缺毋滥；
        strict=False（剧集/动漫）：允许包含式模糊匹配（英文名常只出现在别名里）。
        """
        best: Optional[Tuple[float, Dict[str, Any]]] = None
        for q in queries:
            q = (q or "").strip()
            if len(q) < 2:
                continue
            try:
                subs = self.search(q)
            except BangumiError:
                continue
            nq = _norm(q)
            for s in subs:
                if not isinstance(s, dict):
                    continue
                names = {_norm(s.get(k)) for k in ("name", "name_cn")}
                names.discard("")
                sc = 0.0
                if nq and nq in names:
                    sc += 100.0
                elif nq and any(nq in n or n in nq for n in names):
                    sc += 40.0
                if s.get("type") == 2:  # 2 = 动画
                    sc += 5.0
                if best is None or sc > best[0]:
                    best = (sc, s)
            if best and best[0] >= 100:
                break
        threshold = 100.0 if strict else 40.0
        if not best or best[0] < threshold:
            return None
        return self._shape(best[1])

    def _shape(self, s: Dict[str, Any]) -> Dict[str, Any]:
        """经典搜索接口只有基础字段，补一次 v0 条目详情拿 summary/rating/images。"""
        sid = s.get("id") or s.get("subject_id")
        full = self.subject(sid) if sid else None
        if full:
            s = {**s, **{k: v for k, v in full.items() if v}}
        rating = s.get("rating") or {}
        summary = (s.get("summary") or "").strip()
        images = s.get("images") or {}
        return {
            "bgm_id": sid,
            "name": s.get("name"),
            "name_cn": s.get("name_cn") or None,
            "air_date": s.get("date") or s.get("air_date"),
            "rating": rating.get("score"),
            "votes": rating.get("total"),
            "summary": (summary[:400] + "…") if len(summary) > 400 else (summary or None),
            "cover": images.get("large") or images.get("common") or images.get("medium") or s.get("image"),
            "url": s.get("url") or f"https://bgm.tv/subject/{sid}",
        }
