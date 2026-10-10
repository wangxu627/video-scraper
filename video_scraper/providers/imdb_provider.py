"""（实验性）IMDb 直连：基于 cinemagoer（原 IMDbPY）。

注意：IMDb 没有官方公开 API，cinemagoer 靠解析页面/数据集工作，随时可能因
IMDb 改版而失效。常规用途建议直接用 TMDb（已内置 IMDb ID 映射），本模块只在
需要 IMDb 官方分数/中文别名(AKA) 时作为补充。
"""
from __future__ import annotations

import re
from typing import Any, Dict

CJK_RE = re.compile(r"[\u4e00-\u9fff]")


class ImdbProvider:
    def __init__(self):
        from imdb import Cinemagoer  # 延迟导入：未安装时由调用方给出清晰提示

        self.ia = Cinemagoer()

    def enrich(self, imdb_id: str) -> Dict[str, Any]:
        """按 IMDb ID（如 tt1375666）取分数/票数/年份/中文别名。"""
        out: Dict[str, Any] = {"imdb_id": imdb_id}
        try:
            num = re.sub(r"\D", "", imdb_id or "")
            if not num:
                return {"error": f"无效 IMDb ID: {imdb_id!r}"}
            movie = self.ia.get_movie(num)
            out.update(
                title=movie.get("title"),
                year=movie.get("year"),
                rating=movie.get("rating"),
                votes=movie.get("votes"),
            )
            # 中文别名（AKA），格式如 "盗梦空间::China (Mandarin title)"
            try:
                self.ia.update(movie, info=["akas"])
                akas = movie.get("akas") or []
                out["akas_zh"] = [
                    a for a in akas if CJK_RE.search(str(a).split("::")[0])
                ][:5]
            except Exception:
                pass
            return out
        except Exception as e:
            return {"error": f"IMDb 查询失败: {e}"}
