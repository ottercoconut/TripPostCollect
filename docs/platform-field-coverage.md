# 平台字段覆盖表

本表记录当前项目抓取链路能结构化入库、只能作为页面证据保留、或当前不能提供的字段。它描述的是 TripPostCollect 当前实现能力，不等同于平台本身的全部公开能力。

图例：

- `结构化`：当前链路会写入 `web_posts` 或 `web_post_images` 的明确字段。
- `证据`：页面级抓取会保留在 `ctf_captures`、截图、HTML、正文或图片资源中，但当前不拆成稳定结构化字段。
- `无`：当前链路不抓取、不处理或无法可靠提供。
- `条件`：平台或页面有时提供；当前实现只在明确字段或页面元数据存在时写入。
- `当前未命中`：当前实现有提取逻辑，但本项目已有抓取产物没有拿到可解析的字段值。

| 平台 | 当前链路 | 帖文文本/标题 | 图片 URL/样本 | 视频内容 | 发帖时间 `published_at` | 作者 ID/昵称/头像 | 作者主页 URL | 作者粉丝量 | 作者简介/等级/认证 | 点赞/收藏/评论/分享 | 浏览量 | 平台原始 ID/URL | 备注 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 微博 | MediaCrawler 搜索 | 结构化 | 结构化 URL，已验证 | 无 | 结构化，已验证 | 结构化昵称/哈希 | 无 | 结构化，已验证 | 条件 | 结构化 | 条件 | 结构化 | `store/weibo` 保留 `image_list/image_count` 与 `followers_count/fans_count`；50 条图文完整性已验证。 |
| 小红书 | MediaCrawler 搜索 + 作者主页补充 | 结构化 | 结构化 URL；可保存样本 | 无 | 结构化，已验证 | 结构化 | 结构化 | 结构化 | 条件 | 结构化 | 条件 | 结构化 | 粉丝量是重点字段；低样本搜索会补作者主页指标。 |
| 抖音 | MediaCrawler 搜索 | 结构化 | 结构化 URL | 无 | 结构化，已验证 | 结构化 | 条件 | 条件 | 条件 | 结构化 | 条件 | 结构化 | MediaCrawler 搜索结果已验证；页面级兜底产物未命中。 |
| B站 | 页面级 Opus/图文抓取；手工 article 搜索 | 结构化 | 结构化 URL，已验证 | 无 | 结构化，已验证 | 结构化昵称/ID | 证据 | 无 | 证据 | 结构化点赞/评论 | 结构化 | 结构化 | 默认调度仍走 Opus 详情页；`mediacrawler_crawl.py --platforms bilibili` 手工走 article 搜索，50 条 `烟台旅游` 已验证。 |
| 知乎 | MediaCrawler 搜索 | 结构化 | 结构化 URL，已验证 | 无 | 结构化，已验证 | 结构化昵称/哈希 | 无 | 无 | 无 | 结构化赞/评 | 无 | 结构化 | 依赖已验证 `d_c0/z_c0` cookie 快照；`created_time` -> `published_at`；正文 HTML 图片保存为 `image_list/image_count`。 |
| 携程 | 页面级游记/目的地抓取 | 结构化为页面正文 | 证据；可入 `web_post_images` | 无 | 当前未命中 | 证据 | 证据 | 无 | 证据 | 证据 | 证据 | 结构化为 capture URL | MediaCrawler 不覆盖；已有页面产物未拿到可解析发帖时间。 |
| 去哪儿 | 页面级目的地/游记抓取 | 结构化为页面正文 | 证据；可入 `web_post_images` | 无 | 当前未命中 | 证据 | 证据 | 无 | 证据 | 证据 | 证据 | 结构化为 capture URL | MediaCrawler 不覆盖；已有页面产物未拿到可解析发帖时间。 |
| 豆瓣小组 | 页面级话题抓取 + 作者主页补粉丝量 | 结构化标题与正文 | 证据；可入 `web_post_images`（已过滤 `doubanio.com/f/` 装饰图与验证码 JSON） | 无 | 结构化（`create-time`） | 结构化 | 结构化 | 条件（people 页补全，隐私用户为 NULL） | 条件（`group_name` 写入 `author_description`） | 证据 | 证据 | 结构化为 capture URL | 话题页提取标题/作者五元组/发帖时间/正文；搜索页过滤不入主表；keyword→城市；作者粉丝量需独立访问 people 页。 |

## 发帖时间核对结果

截至 2026-07-06，默认库、临时验证库和本次实测产物中的 `published_at` 命中情况如下：

2026-07-06 本次修正后，新的 `published_at` 入库值统一为 Asia/Shanghai ISO 字符串；默认库中既有旧记录不回填。

| 平台 | 核对来源 | 命中情况 | 结论 |
|---|---|---:|---|
| 微博 | `web_posts` / `mediacrawler_search` | 10/10 | MediaCrawler 当前可结构化提供。 |
| 小红书 | `web_posts` / `mediacrawler_search` | 10/10 | MediaCrawler 当前可结构化提供。 |
| 抖音 | `web_posts` / `mediacrawler_search` | 76/76 | MediaCrawler 当前可结构化提供。 |
| 抖音页面兜底 | `ctf_captures` / `ctf_capture_page` | 0/1 | 页面级兜底当前未命中。 |
| B站 Opus 页面级 | 临时库 `temp/bilibili_publish_verify_shanghai.sqlite` / `ctf_captures` | 2/2 | 页面级 Opus 可见文本日期可结构化提取为 Asia/Shanghai；内容就绪记录会同步进 `web_posts`。 |
| B站 article 搜索 | `web_posts` / `outputs/mediacrawler_runs/20260707T173332+0000/summary.json` | 50/50 | `mediacrawler_crawl.py --platforms bilibili --keyword 烟台旅游 --mediacrawler-max-notes 50 --import-limit 50` 已验证；`pubdate/pub_time` -> Asia/Shanghai `published_at`。 |
| 知乎 | MediaCrawler 实测 `outputs/mediacrawler_runs/20260706T082459+0000/summary.json` | 18/18 | 已通过 `mediacrawler_login_warmup.py` 保存并复用 `d_c0/z_c0` cookie 快照；搜索结果 `created_time` 可结构化为 Asia/Shanghai `published_at`。 |
| 携程 | `outputs/ctf_resource_crawls/**/capture_meta.json` | 0/4 | 页面级链路当前未命中。 |
| 去哪儿 | `outputs/ctf_resource_crawls/**/capture_meta.json` | 0/3 | 页面级链路当前未命中。 |
| 豆瓣小组 | `web_posts` / `import_ctf_captures.py` 的 `extract_douban_topic_fields` | 1/1 | 导入时从话题页 `<span class="create-time">` 提取，解析为 Asia/Shanghai；`capture_meta` 层的通用元数据提取仍为 0/3，粉丝量另走 people 页补全。 |

## 微博图文与粉丝量核对结果

2026-07-06 修正 `tools/MediaCrawler/store/weibo/__init__.py` 后，微博搜索结果会保存 `mblog.pics` 图片 URL 和 `mblog.user` 粉丝数字段。验证命令使用 `mediacrawler_crawl.py --platforms weibo --keyword 济南旅游 --mediacrawler-max-notes 150 --no-import`，原始候选见 `outputs/mediacrawler_runs/20260706T151935+0000/weibo/data/weibo/jsonl/search_contents_2026-07-06.jsonl`。

| 核对项 | 结果 |
|---|---:|
| 原始候选 | 149 |
| 唯一微博 ID | 145 |
| 带作者粉丝量 | 149 |
| 带图片 URL | 79 |
| 可用图文候选 | 79 |
| 最终筛选图文 | 50 |
| 最终 50 条缺粉丝量 | 0 |
| 最终 50 条缺图片 | 0 |
| 最终 50 条缺正文/时间/互动指标 | 0 |

最终筛选文件为 `outputs/mediacrawler_runs/20260706T151935+0000/weibo/usable_50_image_text.jsonl`，校验摘要为 `outputs/mediacrawler_runs/20260706T151935+0000/weibo/usable_50_validation.json`。

## 知乎图文图片核对结果

2026-07-08 修正 `tools/MediaCrawler/media_platform/zhihu/help.py` 和 `tools/MediaCrawler/model/m_zhihu.py` 后，知乎搜索结果会在正文 HTML 清洗为纯文本前提取 `<img>` 的 `data-original` / `data-actualsrc` / `src`，保存为 `image_list/image_count`。公式图片 `zhihu.com/equation` 会跳过，不计入正文图片。

验证命令使用 `mediacrawler_crawl.py --platforms zhihu --keyword 济南旅游 --mediacrawler-max-notes 40 --import-limit 20`，产物见 `outputs/mediacrawler_runs/20260707T193211+0000/summary.json`。

| 核对项 | 结果 |
|---|---:|
| JSONL 内容记录 | 36 |
| 带图片 URL 的 JSONL 记录 | 18 |
| 导入 `web_posts` | 20 |
| 导入后有效图片记录 | 8 |
| `published_at` 命中 | 36 |
| 公式图片误计入 | 0 |

## 字段优先级

高优先级字段包括：帖文文本、图片、平台原始 ID/URL、作者身份、作者粉丝量、作品发表时间、点赞/收藏/评论/分享等互动数。页面级链路无法稳定拆出的字段，仍应保留原始页面证据，后续再清洗或补结构化。

## 维护要求

- 新增平台或改变抓取链路时，同步更新本表。
- 当某字段从“证据”升级为“结构化”时，确认对应导入脚本会写入 `web_posts` / `web_post_images`。
- 不用抓取时间代替作品发表时间；没有明确平台字段或页面元数据时，`published_at` 保持为空。
- 视频内容不进入抓取和入库链路；明确视频目标或视频记录只作为跳过事件处理。
