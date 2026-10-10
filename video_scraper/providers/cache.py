"""轻量磁盘缓存（JSON 文件），避免对同一关键词重复发起在线请求。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional


class DiskCache:
    def __init__(self, path, ttl_days: float = 30.0):
        self.path = Path(path).expanduser()
        self.ttl = ttl_days * 86400.0
        self.data: Dict[str, Any] = {}
        self._dirty = False
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}

    def get(self, key: str) -> Optional[Any]:
        e = self.data.get(key)
        if not isinstance(e, dict) or "ts" not in e:
            return None
        if time.time() - e["ts"] > self.ttl:
            return None
        return e.get("val")

    def set(self, key: str, val: Any) -> None:
        self.data[key] = {"ts": time.time(), "val": val}
        self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[警告] 缓存写入失败: {e}")
