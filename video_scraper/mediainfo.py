"""读取视频内部元信息（MediaInfo，经 pymediainfo 封装）。

Windows 上 pymediainfo 的 wheel 自带 MediaInfo.dll，pip 装完即用；
Linux/macOS 需要系统安装 libmediainfo（apt install libmediainfo0v5 / brew install mediainfo）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional


def _int(v) -> Optional[int]:
    if v is None:
        return None
    m = re.search(r"-?\d+", str(v).replace(",", ""))
    return int(m.group()) if m else None


def _float(v) -> Optional[float]:
    if v is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group()) if m else None


def read_media_info(path: Path) -> Dict[str, Any]:
    """解析容器/流信息；任何失败都不抛异常，返回带 error 字段的 dict。"""
    info: Dict[str, Any] = {"available": False, "engine": "pymediainfo"}
    try:
        from pymediainfo import MediaInfo
    except Exception as e:  # ImportError 或 DLL 缺失
        info["error"] = f"pymediainfo 不可用（{e}）；请 pip install pymediainfo"
        return info

    try:
        mi = MediaInfo.parse(str(path))
    except Exception as e:
        info["error"] = f"MediaInfo 解析失败: {e}"
        return info

    tracks = list(mi.tracks or [])
    gen = next((t for t in tracks if t.track_type == "General"), None)
    vid = next((t for t in tracks if t.track_type == "Video"), None)
    auds = [t for t in tracks if t.track_type == "Audio"]

    if gen:
        duration_ms = _float(gen.duration)
        size_mb = _float(gen.file_size)
        embedded = (getattr(gen, "title", None) or getattr(gen, "movie", None) or "").strip()
        info.update(
            available=True,
            container=gen.format,
            duration_sec=int(duration_ms / 1000) if duration_ms else None,
            file_size_mb=round(size_mb / 1048576, 1) if size_mb else None,
            # 内嵌 title 标签（很多压制/录制的文件里就是干净的片名）
            embedded_title=embedded or None,
        )
    if vid:
        gen_bitrate = _int(getattr(gen, "bit_rate", None)) if gen else None
        info.update(
            width=_int(vid.width),
            height=_int(vid.height),
            video_codec=vid.format,
            video_profile=getattr(vid, "format_profile", None),
            fps=_float(vid.frame_rate),
            bitrate_kbps=_int(vid.bit_rate) or gen_bitrate,
            hdr=getattr(vid, "hdr_format", None) or None,
        )
    if auds:
        a = auds[0]
        info.update(
            audio_codec=a.format,
            audio_channels=getattr(a, "channel_s", None),
            audio_language=getattr(a, "language", None),
            audio_tracks=len(auds),
        )

    if not tracks:
        info["error"] = "MediaInfo 未识别出任何流（文件可能损坏或只是占位文件）"
    return info
