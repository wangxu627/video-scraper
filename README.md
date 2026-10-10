# video-scraper 影视文件信息搜刮器

类似 NAS 影院应用（Jellyfin/Emby/Kodi）里的"刮削器"，但拆成**三阶段管线**，中间用 JSON 文件衔接，
方便"在 A 主机扫描、在 B 主机匹配、再回任意主机执行"：

```
① scan  扫描目录 → guessit 解析文件名 → MediaInfo 读元信息  → scan.json   （离线、无需 key、可跨主机）
② match 读 scan.json → TMDb/Bangumi[/IMDb] 在线匹配         → match.json  （联网，只读不写媒体库）
③ apply 读 match.json → 改名/海报/NFO/字幕                  → apply_report.json（默认 dry-run）
```

支持：电影、剧集、动漫；单文件或目录树；一次多个根目录。

## 工作流程

**① scan（离线）**：递归扫描（跳过样本/隐藏/系统目录）→ guessit 解析（标题/年份/季/集/编码）+
目录名提示 + 视频内嵌 title 标签 → 生成候选标题列表 → 连同容器/流元信息一起写入 `scan.json`。
每个条目记录 `file / relpath / root`，跨主机时按映射换算路径。

**② match（联网）**：候选标题（内嵌标题 > guessit > 目录名 > 清洗后文件名）逐个搜 TMDb
（先电影后剧集、带年份/不带年份两轮、打分选优）→ 取 zh-CN 标题/简介/类型/评分/海报 +
`external_ids` 里的 IMDb ID → 剧集再取 SxxExx 单集信息 → 疑似动漫自动查 Bangumi 补 `name_cn`。
无 key 时自动降级：仅 Bangumi（`--bgm`）或纯离线。

**③ apply（动文件系统）**：按匹配结果重命名（文件+目录，`Season 1`→`Season 01` 自动规范，
搬空的旧目录自动清理）、下载海报（TMDb 优先、Bangumi 封面兜底）、写 Kodi `.nfo`、
下载字幕（OpenSubtitles）。**默认 dry-run 只打印计划，`--execute` 才真正执行**。

## 为什么"中文名"不直接查 IMDb？

IMDb **没有官方公开 API**，中文别名（AKA）覆盖也很不全。业界通行做法（Jellyfin/Emby 同款）：

| 来源 | 中文名能力 | 是否需要 key | 本程序用途 |
|---|---|---|---|
| **TMDb** (themoviedb.org) | 官方 zh-CN 本地化标题/简介，覆盖最全 | 免费 key | 主力（并映射出 IMDb ID） |
| **Bangumi** (bgm.tv) | 动漫 name_cn，中文社区数据质量最高 | 无需 | 动漫补充（实测可用） |
| IMDb | 中文 AKA 很稀疏 | 无官方 API | `--use-imdb` 实验性直连（cinemagoer） |
| 豆瓣 | 中文数据好 | 无官方 API，第三方接口不稳定 | 未内置 |

## 安装

```powershell
cd video-scraper
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

- Windows：`pymediainfo` 的 wheel 自带 `MediaInfo.dll`，装完即用。
- Linux/macOS：需要系统装 `libmediainfo`（`apt install libmediainfo0v5` / `brew install mediainfo`）。

## 配置

配置文件 `~/.video_scraper.json`（可选，所有 key 都可改用环境变量或命令行参数）：

```json
{
  "tmdb_api_key": "TMDb 的 key（免费申请：https://www.themoviedb.org/settings/api）",
  "opensubtitles_api_key": "OpenSubtitles 的 key（免费：https://www.opensubtitles.com/）",
  "opensubtitles_username": "OpenSubtitles 用户名（下载字幕需要）",
  "opensubtitles_password": "OpenSubtitles 密码"
}
```

环境变量等价：`TMDB_API_KEY` / `OPENSUBTITLES_API_KEY` / `OPENSUBTITLES_USERNAME` / `OPENSUBTITLES_PASSWORD`。

> TMDb 中国大陆直连通常不可达，给终端设 `$env:HTTPS_PROXY="http://..."` 即可（requests 自动走）。

## 用法

### 三阶段（推荐）

```powershell
# ① 任意主机离线扫描（无需 key）
python -m video_scraper scan "D:\Movies" "E:\Anime" --min-size-mb 0 -o scan.json

# ② 拷 scan.json 到有网/有 key 的机器上匹配
python -m video_scraper match scan.json -o match.json --tmdb-key xxx

# ③ 先预览计划，确认后真正执行（改名+海报+NFO+字幕）
python -m video_scraper apply match.json --rename --download-poster --write-nfo --download-subtitles
python -m video_scraper apply match.json --rename --download-poster --write-nfo --download-subtitles --execute
```

**跨主机路径映射**（scan 时的库和 apply 时的库不在同一路径）：

```powershell
# 方式A：精确映射旧根 -> 新根（可多次）
python -m video_scraper apply match.json --map-root "D:\Movies=E:\Media\Movies" --rename --execute
# 方式B：所有条目的根都换成一个目录（整库拷贝/挂载点变化场景）
python -m video_scraper apply match.json --base-dir "E:\Media\Movies" --rename --execute
```

### 一条命令跑完（兼容旧用法）

```powershell
python -m video_scraper all "D:\Movies" "E:\Anime" -o report.json --bgm-always
python -m video_scraper all "D:\Movies" --rename --download-poster --execute   # 附带执行阶段3
```

### 常用选项

| 命令 | 选项 | 说明 |
|---|---|---|
| scan | `roots` | 一个或多个根目录（也可单个文件），默认递归 |
| scan | `--min-size-mb` | 小于该体积视为样本/花絮跳过（默认 50） |
| scan | `--ext` | 追加视频扩展名，可多次 |
| match | `--tmdb-key` / `--lang` | TMDb key / 本地化语言（默认 zh-CN） |
| match | `--bgm` / `--bgm-always` | 启用 Bangumi；`--bgm-always` 对所有条目都查 |
| match | `--use-imdb` | （实验）cinemagoer 直连 IMDb |
| apply | `--rename` / `--no-rename-dirs` | 重命名文件；默认目录+文件都改 |
| apply | `--movie-template` / `--episode-template` | 命名模板，如 `"{title_zh} ({year})"` 字段见 `--help` |
| apply | `--download-poster` / `--write-nfo` | 海报 poster.jpg / Kodi nfo |
| apply | `--download-subtitles` / `--subtitle-langs` | 下载字幕（默认 zh-cn 优先，en 兜底） |
| apply | `--map-root` / `--base-dir` | 跨主机路径映射 |
| apply | `--execute` | 真正执行；缺省为 dry-run |

## 输出示例

**scan.json**（每个条目）：

```json
{
  "file": "D:\\Anime\\进击的巨人\\Season 1\\Attack.on.Titan.S01E01.1080p.WEB-DL.mkv",
  "relpath": "进击的巨人/Season 1/Attack.on.Titan.S01E01.1080p.WEB-DL.mkv",
  "root": "D:\\Anime",
  "parsed": { "type": "episode", "title": "Attack on Titan", "season": 1, "episode": 1,
               "candidates": ["Attack on Titan", "进击的巨人"] },
  "media_info": { "duration_sec": 1440, "width": 1920, "height": 1080, "video_codec": "AVC" }
}
```

**match.json**：在 scan 基础上给每个条目加 `lookup`（TMDb/Bangumi/IMDb 匹配结果）和 `status`（ok/not_found/unmatched/error）。

**apply_report.json**：每个动作一行 `{file, action: rename|poster|nfo|subtitle, status: done|planned|skipped|error, detail}`。

实测效果（Bangumi 路径，TMDb 需 key）：

```
进击的巨人/Season 1/Attack.on.Titan.S01E01.1080p.WEB-DL.mkv
  → 进击的巨人 (2013)/Season 01/进击的巨人 - S01E01.mkv + poster.jpg
千与千寻.Spirited.Away.2001.mkv
  → 千与千寻 (2001).mkv + poster.jpg
```

## 测试

```powershell
python tests\make_test_library.py   # 生成 test_library/（5 个小视频）
python -m video_scraper scan test_library --min-size-mb 0 -o scan.json
python -m video_scraper match scan.json -o match.json --bgm-always
python -m video_scraper apply match.json --base-dir <库副本目录> --rename --download-poster   # 先预览
python -m video_scraper apply match.json --base-dir <库副本目录> --rename --download-poster --execute
python tests\live_probe.py          # 无 key 连通性自检（TMDb 应 401=网络通；Bangumi 应返回搜索结果）
```

## 已知限制

- 绝对集数命名（`- 05.mkv`）取不到单集详情；多季合集建议规范成 `剧名/Season 01/` 结构；
- 字幕下载依赖 OpenSubtitles（免费 key+账号，每日有配额），且需要 IMDb ID（即 TMDb 匹配成功）；
- IMDb 直连是实验性的（无官方 API，页面解析可能随 IMDb 改版失效）；
- TMDb 未收录的小众资源会 `not_found`，此时仍保留 guessit/MediaInfo 结果；
- 中国大陆网络访问 TMDb 需代理（Bangumi 一般可直连）。

## 目录结构

```
video-scraper/
├── video_scraper/            # 主包
│   ├── __main__.py           # CLI（scan/match/apply/all 子命令）
│   ├── pipeline.py           # 三阶段管线核心
│   ├── scanner.py            # 目录扫描（记录 relpath/root 供跨主机映射）
│   ├── parsing.py            # guessit 解析 + 中文命名兜底清洗
│   ├── mediainfo.py          # MediaInfo 内部元信息
│   ├── renamer.py            # 命名模板与重命名规划
│   ├── subtitles.py          # OpenSubtitles 字幕下载
│   ├── output.py             # JSON/CSV/NFO/海报
│   └── providers/            # tmdb / bgm / imdb_provider / cache
├── tests/                    # 测试媒体库生成 + 连通性自检
└── requirements.txt
```

## 类似的开源项目（参考）

- **MediaElch**（GPL，C++/Qt）：Kodi 生态刮削管理器，多信息源；
- **tinyMediaManager**：功能最全之一，早期开源、现闭源免费+付费；
- **Jellyfin**（开源）：服务器内置 TMDb 刮削插件；
- **FileBot**：重命名+刮削，付费闭源；
- 组件级：`guessit`（文件名解析事实标准）、`pymediainfo`、`cinemagoer (IMDbPY)`、`tmdbv3api`。
