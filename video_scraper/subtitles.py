"""OpenSubtitles.com 字幕下载客户端（可选功能，需免费 API key + 账号）。

注册：https://www.opensubtitles.com/ -> API Keys 页面创建 key；
下载配额与账号绑定（免费账户每天有限额）。字幕搜索只需 key，下载还需登录 token。

用法（阶段3 apply）：
    python -m video_scraper apply match.json --download-subtitles --execute
key/账号从 --os-* 参数、环境变量（OPENSUBTITLES_API_KEY / OPENSUBTITLES_USERNAME /
OPENSUBTITLES_PASSWORD）或配置文件 ~/.video_scraper.json 读取。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import requests

BASE = "https://api.opensubtitles.com/api/v1"
UA = "video-scraper/0.2 (local media library tool)"


class OpenSubtitlesError(Exception):
    pass


class OpenSubtitlesClient:
    def __init__(self, api_key: str, username: str = "", password: str = ""):
        if not api_key:
            raise OpenSubtitlesError("缺少 OpenSubtitles API key")
        self.username = username or ""
        self.password = password or ""
        self._token: Optional[str] = None
        self.session = requests.Session()
        self.session.headers.update({
            "Api-Key": api_key,
            "Accept": "application/json",
            "User-Agent": UA,
        })

    # ---------- 认证 ----------
    def _login(self) -> str:
        """下载字幕需要用户 token；搜索只需 Api-Key。"""
        if self._token:
            return self._token
        if not (self.username and self.password):
            raise OpenSubtitlesError(
                "下载字幕需要 OpenSubtitles 账号：设置 --os-username/--os-password "
                "（或配置文件 opensubtitles_username/opensubtitles_password）"
            )
        try:
            r = self.session.post(
                f"{BASE}/login",
                json={"username": self.username, "password": self.password},
                timeout=20,
            )
        except requests.RequestException as e:
            raise OpenSubtitlesError(f"网络错误: {e}")
        if r.status_code in (401, 406):
            raise OpenSubtitlesError(f"OpenSubtitles 登录失败(HTTP {r.status_code}): {r.text[:200]}")
        r.raise_for_status()
        token = (r.json() or {}).get("token")
        if not token:
            raise OpenSubtitlesError("OpenSubtitles 登录响应中没有 token")
        self._token = token
        self.session.headers["Authorization"] = f"Bearer {token}"
        return token

    # ---------- 搜索 ----------
    def _search(self, imdb_id: str, languages: List[str]) -> List[Dict[str, Any]]:
        try:
            r = self.session.get(
                f"{BASE}/subtitles",
                params={"imdb_id": imdb_id, "languages": ",".join(languages)},
                timeout=20,
            )
        except requests.RequestException as e:
            raise OpenSubtitlesError(f"网络错误: {e}")
        if r.status_code == 406:
            raise OpenSubtitlesError(f"OpenSubtitles 拒绝请求（key 无效或配额用尽）: {r.text[:200]}")
        if r.status_code == 401:
            raise OpenSubtitlesError("OpenSubtitles 认证失败(401)：API key 无效")
        r.raise_for_status()
        return (r.json() or {}).get("data") or []

    def _pick(self, entries: List[Dict[str, Any]], languages: List[str]) -> Optional[Dict[str, Any]]:
        """按语言优先级挑一个带可下载文件的条目。"""
        wanted = [l.lower() for l in languages]
        fallback = None
        for entry in entries:
            attrs = entry.get("attributes") or {}
            files = attrs.get("files") or []
            if not files:
                continue
            lang = (attrs.get("language") or "").lower()
            if lang in wanted:
                return attrs
            if fallback is None:
                fallback = attrs
        return fallback

    # ---------- 下载 ----------
    def download_srt(self, imdb_id: str, languages: Optional[List[str]] = None) -> Tuple[bytes, Dict[str, Any]]:
        """按 IMDb ID 下载字幕，返回 (字幕字节, 元信息)。找不到时返回 (b"", {"reason": ...})。"""
        languages = languages or ["zh-cn"]
        self._login()
        entries = self._search(imdb_id, languages)
        attrs = self._pick(entries, languages)
        if not attrs:
            return b"", {"reason": "未找到匹配字幕"}
        files = attrs.get("files") or []
        file_id = files[0].get("file_id")
        if not file_id:
            return b"", {"reason": "字幕条目缺少 file_id"}
        try:
            r = self.session.post(
                f"{BASE}/utilities/download",
                json={"file_id": str(file_id), "sub_format": "srt"},
                timeout=30,
            )
        except requests.RequestException as e:
            raise OpenSubtitlesError(f"网络错误: {e}")
        if r.status_code == 406:
            raise OpenSubtitlesError(f"下载被拒绝（多半是每日配额用尽）: {r.text[:200]}")
        r.raise_for_status()
        link = (r.json() or {}).get("link")
        if not link:
            raise OpenSubtitlesError("下载响应中没有 link")
        content = self.session.get(link, timeout=60).content
        meta = {
            "file_id": file_id,
            "language": attrs.get("language"),
            "release": attrs.get("release"),
            "subtitles_count": len(entries),
        }
        return content, meta
