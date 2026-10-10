"""video_scraper —— 本地影视文件信息搜刮器。

流程：目录扫描 -> 文件名解析(guessit) -> 内部元信息(MediaInfo)
      -> TMDb 在线匹配(中文标题/简介/IMDb 映射) -> 动漫补充 Bangumi(bgm.tv)
      -> 输出 JSON/CSV，可选写 Kodi .nfo 与下载海报。
"""

__version__ = "0.2.0"
