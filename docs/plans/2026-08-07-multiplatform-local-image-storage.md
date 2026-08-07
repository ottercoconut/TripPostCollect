# 五平台正文图片本地存储工程实现方案

> 状态：主程序阶段 I-00 至 I-14 已完成，`MAIN_PROGRAM_READY=true`；历史数据阶段 H-00 至 H-03
> 已完成，H-04 正由后台 worker 执行，H-05 至 H-08 等待同一状态机顺序推进，
> `HISTORICAL_DATA_COMPLETE=false`。主程序验收证据见
> [`2026-08-07-image-main-program-i14-report.md`](2026-08-07-image-main-program-i14-report.md)，历史输入冻结证据见
> [`2026-08-07-historical-image-h00-input-freeze.md`](2026-08-07-historical-image-h00-input-freeze.md)，
> 历史工具与副本演练证据见
> [`2026-08-07-historical-image-h01-tooling-report.md`](2026-08-07-historical-image-h01-tooling-report.md)，
> 默认库备份、容量与执行门禁见
> [`2026-08-07-historical-image-h02-execution-gate.md`](2026-08-07-historical-image-h02-execution-gate.md)，
> 小红书历史数据验收见
> [`2026-08-07-historical-image-h03-xhs-acceptance.md`](2026-08-07-historical-image-h03-xhs-acceptance.md)。
>
> 基线日期：2026-08-07。
>
> 适用范围：B站 article、微博图文、小红书图文、抖音图文、知乎 answer/article。
> 当前约束：只处理正文图片；视频、音乐、视频封面和作者头像不进入本次本地化范围。

## 1. 结论与固定决策

五个平台都已经具备正文图片 URL 发现能力，可以在不改变现有文本抓取、分页和候选记忆语义的
前提下增加本地图片存储。实现必须遵守以下固定决策：

1. 正式抓取从“保存图片 URL”升级为“保存图片 URL，并完整保存全部正文图片到本地”。任一正式
   有效记录只要有一张正文图片未完成本地化，就不能入库，也不能兑现新增目标。
2. `--get-media` 继续作为禁止使用的原始媒体开关；项目只开放语义明确的
   `--download-images`。任何正式路径都不得下载视频、音乐或音频。
3. 图片字节由拥有当前登录态和平台客户端的进程获取：B站由根项目 article 分支获取；微博、
   小红书、抖音和知乎由对应 MediaCrawler 平台进程获取。
4. 平台进程只把文件写入本轮 staging，并生成逐图片 manifest；根项目负责重新校验、晋升到长期
   目录、写入 SQLite 和生成完成证据。
5. 长期文件固定保存在 `data/media/`。`outputs/` 仍是本轮 staging 和审计产物，不作为新实现的
   长期主存储。
6. `web_post_images` 继续作为帖子与本地文件的最终关系表。现有 `local_path`、尺寸、MIME 和
   SHA-256 字段足以表达成功结果，本期不增加主库成功态表。
7. 图片选择必须改为平台显式字段映射；正式入库和下载均不得继续使用递归扫描任意 URL 字段的
   `dedupe_image_urls()`。
8. `image_index` 改为同一 `image_role` 内从 0 开始的来源顺序；正文图身份使用平台稳定资源键，
   不能仅依赖可能过期或变化的完整 URL。
9. 图片下载失败属于可恢复运行失败时，当前来源页不得推进，候选不得进入持久已处理集合；不得
   降级成只保存 URL。
10. 历史补下载不兑现 `target_new_posts`，不修改发现 checkpoint、候选记忆或来源耗尽状态。

## 2. 当前实现与库存基线

### 2.1 当前调用链

```mermaid
flowchart LR
    A[crawl_runner / xhs_runner] --> B[mediacrawler_crawl]
    B --> C[B站自有 article API]
    B --> D[MediaCrawler 平台进程]
    C --> E[JSONL / 正式记录]
    D --> E
    E --> F[collect_formal_records]
    F --> G[import_valid_records]
    G --> H[web_posts]
    G --> I[web_post_images URL 关系]
```

当前 `scripts/mediacrawler_crawl.py` 会递归扫描记录中的 `cover`、`image`、`img`、`pic`、
`avatar` 和 `note_download` 等键，再把命中的 URL 写入 `web_post_images`。这种通用扫描适合早期
字段探索，不适合作为正式正文图片契约：它会把封面、作者主页 URL、音乐或视频下载地址误归为
正文图，也会把同一图片的多个 CDN 变体保存为多张图片。

当前 upsert 会删除并重建同一帖子的全部 `web_post_images`。除小红书外，本地元数据只按完整 URL
保留；抖音等平台的签名 URL 更新后会失去本地文件对应关系。

### 2.2 2026-08-07 默认库审计快照

以下统计来自 `data/trippostcollect.sqlite` 和已完成的小红书本地路径回填报告，只用于确定迁移
边界，不作为未来固定数量：

| 平台 | 帖子 | 当前 `content` 行 | 平台权威正文图 | 已有本地路径 | 需要处理的问题 |
|---|---:|---:|---:|---:|---|
| B站 | 3,006 | 18,050 | 18,050 | 0 | 当前关系与详情 `image_urls` 数量一致 |
| 微博 | 1,007 | 6,384 | 6,384 | 0 | 当前关系与 `image_list` 全部一致 |
| 抖音 | 461 | 4,991 | 4,528 | 0 | 误含 461 封面、1 视频 URL、1 音乐 URL |
| 知乎 | 437 | 10,767 | 10,765 | 0 | 误含 2 条作者主页 URL；另有 437 张作者头像 |
| 小红书 | 1,808 | 21,040 | 17,416 | 17,150 | 多个 CDN 变体被展开；17 帖共 266 张尚未建立本地关系 |

五平台权威正文图合计 57,143 张。当前已建立本地路径 17,150 张，剩余待本地化 39,993 张，
其中 B站、微博、抖音、知乎合计 39,727 张，小红书缺口 266 张。

小红书现有回填的数据库备份、报告和逐文件 SHA-256 证据必须保留；新实现应读取并晋升这些文件，
不能重新下载已经验证成功的 17,150 张图片。

## 3. 目标与非目标

### 3.1 目标

- 五个平台的每条正式图文记录保存全部权威正文图片。
- SQLite 中每个正文图片行都能唯一对应本地文件、来源顺序、来源 URL 和稳定资源键。
- 本地文件经过类型、体积、尺寸和 SHA-256 校验，支持幂等重跑。
- 正式摘要、冻结阶段和数据库验收能证明图片已经落盘，而不是只证明 URL 非空。
- 抓取过程保持图片专用，不触发任何视频或音乐下载。
- 支持对默认库进行可恢复、分批、先 dry-run 后 apply 的历史补下载。
- URL 查询参数、签名或 CDN 域名变化时，已验证的本地图片关系不会被普通 upsert 清空。

### 3.2 非目标

- 不下载作者头像；`author_avatar` 关系继续只保存 URL。
- 不下载页面截图、页面证据层图片或 `ctf_capture_images` 中的远程资源。
- 不做图片 OCR、内容识别、去水印、转码或压缩。
- 不做跨帖子物理去重、硬链接或对象存储；本期只记录 SHA-256，为以后去重保留条件。
- 不改变关键词、分页、候选硬上限、完成模式、作者字段或粉丝来源。
- 不把历史补下载结果计入新抓取数量，不重置任何抓取记忆。

## 4. 目标架构

```mermaid
flowchart LR
    A[平台权威记录] --> B[显式正文图投影]
    B --> C[平台会话内下载]
    C --> D[本轮 staging]
    C --> E[image_manifest.jsonl]
    D --> F[根项目重新校验]
    E --> F
    F --> G[data/media 不可变文件]
    G --> H[正式字段与本地化门禁]
    H --> I[SQLite 事务 upsert]
    I --> J[persistence_verified]
```

该架构把职责分成两层：

- 平台层拥有登录态、Cookie、签名 URL 和平台专用请求头，只负责可靠获取字节并产出 manifest。
- 根项目拥有正式字段契约、长期路径、文件安全、SQLite 关系、摘要和冻结状态，只负责验证和提交。

平台层不得直接修改主库；根项目不得把 Cookie、Storage State 或完整请求头写入 manifest。

## 5. 正文图片投影契约

新增根项目函数：

```python
content_image_candidates(platform_key: str, record: dict[str, Any]) -> list[ImageCandidate]
```

`ImageCandidate` 至少包含：

```text
platform_key
platform_post_id
image_role = content
source_index
source_url
source_key
source_asset_key
```

平台字段固定如下：

| 平台 | 权威输入 | 规则 |
|---|---|---|
| B站 | 详情补全后的 `image_urls` | 仅接受 `content_images_detail_status=detail_observed` 的详情图片 |
| 微博 | store 归一后的 `image_list` | 来源必须是 `mblog.pics`，保持原顺序并去重 |
| 小红书 | `image_list[]` | 每个图片对象只选择 `url_default`、`url`、`url_pre` 中优先级最高的一条 |
| 抖音 | `note_download_url` | 按逗号拆分图文列表；明确排除 `cover_url`、`video_download_url`、`music_download_url` |
| 知乎 | `image_list` | 仅正文 HTML 图片；继续排除 `/equation?` 公式图片 |

所有 URL 先统一处理协议相对地址、空白和明显尾部标点。正文图去重只在同一帖子内进行，并保持
第一次出现的正文顺序。`post_images_count` 必须等于投影后的 `content` 数量。

正式路径完成迁移后，`dedupe_image_urls()` 不再参与 `row_for_record()` 和
`validate_formal_record()`。如果没有其他当前调用方，按项目工程规则直接删除，不保留旧兼容分支。

## 6. 稳定资源身份

完整 URL 仍保存在 `image_url`，但匹配本地文件时使用 `source_asset_key`：

| 平台 | 稳定键优先级 |
|---|---|
| B站 | `bfs` 资源路径中的资产哈希；否则规范化 host + path，忽略查询参数和图片变换后缀 |
| 微博 | `mblog.pics[].pid`；缺失时使用规范化 host + path |
| 小红书 | 现有 `/notes_pre_post/`、`/notes_post/`、`/notes/` 路径身份；否则 host + path |
| 抖音 | `images[].uri` 等原始资源 ID；缺失时使用签名 URL 的规范化 path，忽略查询参数 |
| 知乎 | 原图 URL 的规范化 path；去除尺寸/格式变换部分后生成键 |

键格式固定带平台前缀，例如 `weibo:pid:<pid>`、`douyin:uri:<uri>`。无法取得平台资产 ID 时，
使用规范化 URL 的 SHA-256，不使用 Python 进程随机 hash。

抖音当前归一化记录只保留逗号字符串，需要在 MediaCrawler store 中额外保留不含敏感凭据的
`image_assets` 元数据，以便根项目取得 `uri` 和来源顺序。微博 manifest 同样直接记录 `pid`，
不依赖保存后的纯 URL 反推。

## 7. 下载 manifest 契约

每个平台每轮生成一个 UTF-8 JSONL 文件：

```text
<batch_dir>/<platform>/data/<mediacrawler-key>/image_manifest.jsonl
```

每行对应一张权威正文图，schema v1 如下：

```json
{
  "schema_version": 1,
  "platform_key": "douyin",
  "platform_post_id": "123456",
  "image_role": "content",
  "source_index": 0,
  "source_key": "note_download_url",
  "source_asset_key": "douyin:uri:example",
  "source_url": "https://example.invalid/image",
  "fetch_status": "downloaded",
  "attempts": 1,
  "http_status": 200,
  "staging_path": "douyin/images/123456/000.bin",
  "size_bytes": 1024,
  "mime_type": "image/jpeg",
  "width": 1080,
  "height": 1440,
  "sha256": "64位十六进制",
  "error_code": null
}
```

约束：

- `staging_path` 必须相对本轮平台数据目录，不能是绝对路径或包含 `..`。
- 成功行必须同时有文件、大小、MIME、尺寸和 SHA-256；失败行不得伪造这些字段。
- manifest 不保存 Cookie、请求头、Storage State、作者原始身份或响应正文。
- 控制台和摘要不打印带签名的完整 URL，只打印 host、资源键摘要和错误码。
- 同一 `(platform_key, platform_post_id, image_role, source_index)` 只能有一个最终状态；重试事件
  放在平台日志，manifest 保存最终结果和总尝试次数。
- 根项目必须重新计算 SHA-256、真实类型和尺寸，不能信任平台进程单方面提供的值。

## 8. staging、长期路径与文件安全

### 8.1 路径

在 `trippostcollect.core.paths` 增加：

```text
LOCAL_MEDIA_ROOT = DATA_ROOT / "media"
IMAGE_MATERIALIZATION_RUNTIME = RUNTIME_ROOT / "image_materialization"
```

长期路径固定为：

```text
data/media/<platform>/<safe_platform_post_id>/<source_index:03d>-<sha256前16位>.<真实扩展名>
```

`local_path` 保存相对项目根目录的 POSIX 路径。帖子 ID 只允许 ASCII 字母、数字、下划线和连字符；
其他字符编码为其 SHA-256 前缀，禁止把平台文本直接拼成目录名。

### 8.2 写入与晋升

1. 平台进程在 staging 同目录写入随机名 `.part`。
2. 下载完成后检查体积和文件头，再以 `os.replace()` 原子改为 staging 正式文件。
3. 根项目重新验证 manifest 与文件。
4. 根项目按内容 SHA-256 生成不可变长期路径；目标已存在且 SHA 一致时直接复用。
5. 目标不存在时先复制到长期目录的 `.part`，`fsync` 后原子改名。
6. 不覆盖 SHA 不一致的已有文件；出现冲突立即失败并保留证据。
7. SQLite 事务只引用已经存在且验证通过的长期文件。

文件晋升与 SQLite 无法组成单一原子事务，因此长期文件采用内容哈希不可变命名。数据库提交失败
最多产生无引用文件，不会破坏已有关系；无引用文件由独立、默认 dry-run 的垃圾回收工具处理。

### 8.3 允许类型与限制

- 首期只接受魔数和解码器都能确认的 JPEG、PNG、WebP、GIF 和 AVIF。
- SVG、HTML、JSON、音视频和 `application/octet-stream` 不得作为正式正文图片完成。
- 单文件上限使用独立的归档图片限制；初始值 20 MiB。超过限制记录
  `image_too_large`，不截断保存。
- 解码前限制响应体，解码后限制最大像素数。初始上限为 100,000,000 像素；H-04 处理到经魔数、
  Pillow、MIME 和完整性确认的 B站 JPEG（11,249×10,000，112,490,000 像素）后，经代码、测试和
  治理变更把归档上限调整为 150,000,000 像素。20 MiB 字节上限保持不变，超过任一上限仍记录
  `image_too_large`，不得截断或重编码后冒充原图。
- 对重定向后的每个 URL 重新执行 scheme、host 和本地/私网地址检查。
- PIL 仅用于验证和读取尺寸，不对原图重编码。

现有 `image_proxy.py` 的 URL、重定向、Referer、类型和体积校验应抽为共享的只读验证组件；批量
归档不得直接调用面向管理端预览的 `fetch_remote_image_preview()`，因为归档需要平台会话、重试、
staging、manifest 和更完整的错误分类。

## 9. SQLite 持久化语义

### 9.1 主表不新增字段

成功后的 `web_post_images` 固定写入：

| 字段 | 值 |
|---|---|
| `image_index` | `image_role` 内的 `source_index` |
| `image_url` | 当前平台权威 URL |
| `image_role` | 本期正式图片固定为 `content` |
| `local_path` | 项目相对长期路径 |
| `width` / `height` | 根项目验证后的真实尺寸 |
| `mime_type` | 根项目验证后的真实 MIME |
| `sha256` | 根项目重新计算的 SHA-256 |
| `raw_image_json` | 来源键、稳定资源键、下载 URL、manifest 位置和本地文件证据 |

`raw_image_json` 的目标结构：

```json
{
  "source_key": "image_list",
  "source_index": 0,
  "source_asset_key": "weibo:pid:example",
  "local_file": {
    "source": "formal_image_materialization_v1",
    "source_url": "https://example.invalid/image",
    "manifest_path": "outputs/.../image_manifest.jsonl",
    "manifest_line": 1,
    "size_bytes": 1024
  }
}
```

尺寸、MIME、SHA 和路径以表字段为查询权威，JSON 只保存来源审计信息。

### 9.2 upsert 改造

`upsert_web_post()` 重建图片行前必须建立旧行索引，匹配顺序固定为：

1. 相同 `image_role + source_asset_key`；
2. 相同 `image_role + 规范化 URL`；
3. 仅对没有稳定键的旧数据，在同一权威来源序列中使用 `image_role + source_index`，且必须验证
   来源 URL 身份一致。

匹配成功且本地文件仍存在、SHA 一致时，允许更新 `image_url` 并保留本地字段。稳定键变化、文件
缺失或 SHA 不一致时清空旧本地字段，必须重新本地化。删除后重建图片行仍可保留，但不能再以完整
签名 URL 作为唯一保护条件。

每个帖子在一个 SQLite 事务中完成主表和全部图片行更新。帖子只有在全部权威正文图均有验证后的
本地元数据时才能提交；禁止一张成功、一张失败后提交半个帖子。

### 9.3 无状态表的失败恢复

主库只保存成功关系。失败和重试状态保存在本轮 manifest、summary 和
`data/runtime/image_materialization/` 报告中：

- 新记录失败时不进入 `web_posts`，下一轮由未推进的来源前沿重新处理。
- 历史记录以 `local_path IS NULL`、文件缺失或 SHA 不一致作为待处理集合，重复执行即可恢复。
- 每次历史 apply 前创建 SQLite 一致性备份；报告记录输入集合摘要、映射摘要和前后数据库 SHA。

本期不增加下载任务表，避免与“失败新记录尚无 `web_post_images.id`”以及现有删除重建语义形成
第二套身份系统。如果后续需要跨主机任务队列，再单独设计持久队列表。

## 10. 平台实现

### 10.1 B站

- 所有权继续在根项目 `run_bilibili_article_search()`，不进入 MediaCrawler 视频分支。
- 使用 `extract_bilibili_detail_images()` 产生的详情图片序列；搜索预览图不得下载为正文图。
- 下载使用当前 article Cookie、文章 Referer 和浏览器 User-Agent。
- 在详情和作者字段通过初步门禁后下载，下载完整后记录才进入正式有效集合。
- 稳定键优先使用 B站 `bfs` 资产路径哈希。
- MediaCrawler 的 `BilibiliVideo` 和 `get_bilibili_video()` 不改造成图片入口，也不得被正式命令调用。

### 10.2 微博

- 继续使用 `mblog.pics` 和 `wb_client.get_note_image()`。
- `get_note_images()` 接收微博 ID，并按 `pics` 原始顺序传递 `source_index`、`pid` 和 URL。
- 取消只按 `<picid>.<url后缀>` 平铺的正式存储语义；staging 改为逐帖目录。
- 扩展名由响应和文件头确定，不使用 `url.split(".")[-1]`。
- 只有正式字段初步有效的图文候选才下载；已知 ID 继续在媒体处理前跳过。

### 10.3 小红书

- 保留现有 `get_notice_media()` 的图片专用语义和禁用视频分支。
- `get_note_images()` 不再把所有文件强制命名为 `.jpg`，改为真实格式和 manifest。
- 每个 `image_list` 对象只下载一个权威 URL，避免 `url_default/url_pre` 变体重复入库。
- 先复用已验证的 17,150 个本地文件；17 帖共 266 张缺口通过正式账号会话重新取得详情后补齐。
- XHS 正式 runner 始终要求本地图片，不再把 `download_images` 作为可以关闭的业务能力。

### 10.4 抖音

- 正文图只来自 `_extract_note_image_list()` 和原始 `images[]` 资产元数据。
- 项目图片模式直接调用 `get_aweme_images()`；如果图片列表为空就返回，不得调用
  `get_aweme_video()`。
- `cover_url`、`video_download_url` 和 `music_download_url` 永远不进入候选、manifest 或本地目录。
- 新抓记录必须在当前 `dy_client` 仍存活时立即下载，避免签名 URL 过期。
- 历史 URL 先尝试直接下载；403、签名错误或 URL 过期时按 `aweme_id` 请求一次新详情，仅刷新图片
  资产列表，再下载并更新 `image_url`。刷新不得重新跑关键词发现或推进 checkpoint。
- 只有刷新后的详情仍明确不存在该图片，且作品状态给出决定性证据时，才能判永久无效；普通
  403、429、5xx、超时和解析失败均为可恢复失败。

### 10.5 知乎

- 继续使用 `extract_image_urls_from_html()` 的 `data-original`、`data-actualsrc`、`src` 优先级和
  公式排除规则。
- 在 Zhihu MediaCrawler client 中增加图片字节获取方法，复用当前 Cookie、User-Agent 和内容页
  Referer；新增知乎图片 staging store 和 manifest。
- 搜索响应有图时可直接下载；搜索缺图并完成详情补全时，必须下载详情合并后的 `image_list`。
- `avatar_url` 和 `author_profile_url` 不进入正文图片投影。

## 11. 根项目执行器改造

### 11.1 CLI 语义

- `scripts/mediacrawler_crawl.py --download-images` 扩展为五平台安全图片模式。
- 新增 `--media-root` 作为测试和诊断的受控路径覆盖；正式 runner 固定冻结并使用
  `LOCAL_MEDIA_ROOT`，不能把临时路径带入正式完成。
- `--get-media` 继续立即失败，错误信息明确说明只能使用项目图片模式。
- 通用 `crawl_runner.py` 为所有正式 `mediacrawler_search` child 强制加入 `--download-images`。
- `xhs_runner.py` 的正式计划固定 `local_image_storage_required=true`，正式 child 强制加入该参数。
- `config/xhs_targets.json` 及当前仍在使用的同 schema one-off 配置删除可选
  `download_images` 字段；本地图片是正式契约，不再由目标配置决定是否开启。
- `config/crawl_targets.json` 不新增逐 job 图片开关，避免同一正式 profile 同时存在 URL-only 和
  本地化两种完成语义。
- `--no-import` 诊断允许不下载图片；若显式传 `--download-images`，只生成 staging 和 manifest，
  不晋升长期目录、不写主库，也不能作为正式完成证据。

正式模式缺少 `--download-images` 不是可降级配置，而是计划或命令构造错误。

### 11.2 验证与导入顺序

`mediacrawler_crawl.py` 主流程调整为：

1. 执行平台抓取并取得记录、staging 和 manifest。
2. 运行现有行为、策略、分页和字段校验。
3. 对准备进入有效集合的记录执行平台显式正文图投影。
4. 校验每帖 manifest 的资源键、顺序、数量、文件路径和字节元数据。
5. 将通过的文件晋升到 `data/media/`，形成 `MaterializedImage`。
6. 把 `MaterializedImage` 注入待入库记录。
7. 只有字段目标和图片本地化目标同时满足时执行 `import_valid_records()`。
8. 写摘要、发现 checkpoint 和最终退出状态。

不能先提交数据库再补路径。晋升成功但 SQLite 失败时保留不可变文件并报告无引用文件；下一次
相同内容可直接复用。

### 11.3 摘要字段

child summary 新增：

```json
{
  "image_materialization": {
    "required": true,
    "candidate_posts": 10,
    "expected_images": 86,
    "downloaded_images": 86,
    "reused_images": 0,
    "promoted_images": 86,
    "retryable_failures": 0,
    "terminal_failures": 0,
    "complete": true,
    "manifest_paths": [],
    "manifest_sha256": "..."
  }
}
```

`formal_validation` 新增：

```text
local_images_required
local_images_complete
valid_local_image_count
invalid_local_image_reason_counts
```

标准错误码至少包括：

```text
missing_image_manifest
image_manifest_count_mismatch
image_manifest_identity_mismatch
image_path_escape
image_file_missing
image_too_large
image_non_raster_response
image_decode_failed
image_hash_mismatch
image_download_retryable
image_refresh_failed
image_promotion_conflict
```

摘要只保存计数、受控路径、稳定键摘要和错误码，不输出 Cookie 或带敏感查询参数的完整 URL。

## 12. 正式完成与冻结阶段

不新增第六个冻结阶段，图片证据并入现有五阶段：

| 阶段 | 新增图片门禁 |
|---|---|
| `plan_frozen` | 冻结 `local_image_storage_required=true`、长期目录和图片限制版本 |
| `command_executed` | 证明执行的是项目图片模式，而不是原始媒体模式 |
| `artifacts_verified` | manifest 存在、SHA 固定、staging 文件逐项验证通过 |
| `persistence_verified` | SQLite 正文图片数、本地路径、文件存在、SHA、尺寸和 MIME 全部一致 |
| `task_finalized` | 图片本地化完成谓词与原数量/来源耗尽谓词同时成立 |

默认数量模式和显式来源耗尽模式都必须满足 `local_images_complete=true`。图片失败时：

- 可恢复错误：child 停止为 `runtime_failed`，当前页保持未完成，候选不写入已处理集合，不入库。
- 决定性永久无效：候选可以作为无效候选继续，但必须有平台详情证据，不能只凭 CDN 404/403。
- 不允许 `local_path=NULL` 的正式正文图通过 `persistence_verified`。
- 不允许用“文件已在 staging”替代长期文件和数据库关系验证。

实现阶段需要同步更新受限冻结的 `formal-crawl-contract.md` 和 `crawl-architecture.md`，并更新冻结
哈希。该治理变更必须与代码、测试和平台文档在同一实施批次完成，不能只改其中一层。

## 13. 历史数据清理与补下载

本章只定义主程序总验收后的第二阶段工作，不与主程序开发并行执行。第 18 节
`MAIN_PROGRAM_READY` 门禁完成前：

- 不实现或运行默认库历史补下载；
- 不修改默认库现有图片关系；
- 不把现有小红书文件晋升到新的长期目录；
- 不以历史数据是否补齐阻塞单个平台主程序模块的开发；
- 所有关系修复、upsert 和晋升测试只使用 fixture、临时 SQLite 和临时媒体目录。

主程序总验收后先重新盘点当时的默认库，再冻结历史补全输入。以下 2026-08-07 数量只是容量规划
基线，不能代替第二阶段开始时的实时盘点。

新增正式历史入口：

```text
scripts/materialize_local_images.py
```

该入口默认 dry-run；写默认库必须显式 `--apply`，并自动创建 SQLite backup。它不得调用正式
runner，不得改变任何抓取记忆。

### 13.1 前置关系修复

1. 从每条 `raw_sample_json` 按第 5 节重新生成权威正文图序列。
2. 输出逐平台预期行数、删除误分类数、合并变体数和 URL 规范化数。
3. 在临时 SQLite 执行关系重建，验证主表行数不变、外键和唯一索引通过。
4. 保留已有、可验证且稳定键一致的本地字段。
5. 默认库 apply 前创建备份，并对修复前后每帖正文和非图片字段做哈希不变量校验。

当前库的预期修复结果：

- 抖音正文图片从 4,991 调整为 4,528，移除 463 条非正文媒体 URL。
- 知乎正文图片从 10,767 调整为 10,765，移除 2 条作者主页 URL。
- 小红书正文图片从 21,040 折叠为 17,416 个权威图片对象，并保留已验证的 17,150 个本地关系。
- 微博数量保持 6,384。
- B站数量保持 18,050。

### 13.2 分平台补下载

执行顺序固定为：

1. 小红书已有文件晋升与 266 张缺口补齐；
2. B站；
3. 微博；
4. 知乎；
5. 抖音。

每个平台先固定 10 帖小样，再按受控批次运行。每批报告：计划帖子、计划图片、复用、下载、晋升、
更新、待重试、永久无效、字节总量、耗时、数据库备份和 SHA。只有当前批次每帖全部图片完成时才
更新该帖；不提交部分帖子关系。

抖音历史批次必须额外报告：旧 URL 直取成功、详情刷新成功、详情刷新失败和决定性永久无效。

### 13.3 现有小红书回填资产

`scripts/backfill_local_image_paths.py` 和
`src/trippostcollect/artifacts/local_image_backfill.py` 已经完成“现有文件到数据库”的严格映射，
继续作为这次迁移的输入和审计证据，不作为以后正式抓取路径。新历史入口复用其项目内路径、
SHA、MIME、尺寸和原始 JSONL 对应能力，再把已验证文件晋升到长期目录。

## 14. 重试、节流与并发

- 正式抓取保持现有浏览器和 API 并发规则；图片下载使用独立的小并发，不扩大搜索并发。
- 默认同一帖子内串行，平台级最多 2 个图片请求；抖音和小红书首期固定为 1。
- 单图片最多 3 次：网络错误、超时、429 和 5xx 使用带随机抖动的指数退避。
- 401/403 不直接判永久失败；抖音先刷新详情 URL，其他平台先刷新当前客户端 Cookie 或重试一次
  带平台 Referer 的请求。
- `Content-Length` 超限时不读取完整响应；没有长度时流式读取并在上限处终止。
- 图片请求计数和耗时写入 `image_materialization`，但不改变现有站点正式会话数量语义。
- 磁盘预检必须同时考虑 staging 和长期文件。历史全量前先用样本估算 p50/p95 文件大小，要求可用
  空间至少覆盖估算总量的 2 倍和安全余量。

## 15. 并发、幂等与垃圾回收

- 使用 `LOCK_DIR` 下的 `(platform, platform_post_id)` 文件锁，防止两个 child 同时晋升同一帖子。
- 内容哈希路径和 `os.replace()` 保证重复下载不会生成不同成功文件。
- 同一批次重复执行时，目标文件与 SHA 一致则计为 `reused_images`，不重新写入。
- 禁止在普通 upsert 中删除长期文件；正文图片被平台移除时只删除数据库关系，旧文件进入无引用态。
- 新增独立 `scripts/gc_local_images.py`，默认只输出无引用文件和总字节数。只有显式 `--apply`、完成
  数据库备份并再次确认引用集合后才允许删除；首期上线阶段不自动执行 GC。

## 16. 代码变更清单

### 16.1 根项目

| 文件 | 变更 |
|---|---|
| `src/trippostcollect/core/paths.py` | 增加长期图片根目录和图片运行状态目录 |
| `src/trippostcollect/artifacts/image_candidates.py` | 新增五平台显式投影、URL 规范化和稳定资源键 |
| `src/trippostcollect/artifacts/image_materialization.py` | manifest 校验、文件验证、晋升和数据库元数据构造 |
| `src/trippostcollect/artifacts/image_proxy.py` | 抽取可复用的远程响应安全校验，不承担批量归档编排 |
| `scripts/mediacrawler_crawl.py` | 替换递归图片提取、扩展安全图片模式、加入本地化门禁与摘要 |
| `scripts/crawl_runner.py` | 正式结构化 child 强制图片模式；冻结与验收图片证据 |
| `scripts/xhs_runner.py` | 正式 XHS 固定要求本地图片；验收 manifest 和本地关系 |
| `config/xhs_targets.json` | 删除可选 `download_images`，正式 XHS 固定要求本地图片 |
| 当前有效的 XHS one-off 配置 | 按同一 schema 删除可选 `download_images`，不保留旧兼容字段 |
| `scripts/materialize_local_images.py` | 第二阶段新增历史清理、补下载、恢复和 apply 入口；主程序总验收前不实现、不运行 |
| `scripts/gc_local_images.py` | 第二阶段新增默认 dry-run 的无引用文件审计/清理入口 |
| `src/trippostcollect/artifacts/local_image_backfill.py` | 第二阶段复用现有 XHS 证据并支持向长期目录晋升 |

### 16.2 MediaCrawler 定向补丁

| 文件 | 变更 |
|---|---|
| `tools/MediaCrawler/tools/image_manifest.py` | 新增原子 staging 写入和 JSONL manifest 公共实现 |
| `media_platform/weibo/core.py` | 传递微博 ID、pid、顺序和来源 URL；只下载初步有效图文 |
| `store/weibo/weibo_store_media.py` | 逐帖 staging、真实格式、返回 manifest 元数据 |
| `media_platform/douyin/core.py` | 新增严格 images-only 路径，删除正式图片模式的视频回退 |
| `store/douyin/__init__.py` | 保留 `images[]` 稳定资产元数据 |
| `store/douyin/douyin_store_media.py` | 原子 staging、真实格式和 manifest |
| `media_platform/zhihu/core.py` | 在正文/详情图片确定后下载并汇总状态 |
| `media_platform/zhihu/client.py` | 新增复用当前会话的图片字节请求 |
| `store/zhihu/zhihu_store_media.py` | 新增知乎图片 staging 和 manifest |
| `media_platform/xhs/core.py` | 每个图片对象只取一个 URL，真实格式、失败分类和 manifest |
| `store/xhs/xhs_store_media.py` | 原子 staging、真实格式和 manifest |

B站第三方视频下载代码不属于本功能修改范围。

### 16.3 文档与治理

实现完成时同步更新：

- `docs/README.md`
- `docs/data-persistence.md`
- `docs/platform-field-coverage.md`
- `docs/platforms/bilibili.md`
- `docs/platforms/weibo.md`
- `docs/platforms/douyin.md`
- `docs/platforms/zhihu.md`
- `docs/platforms/xhs.md`
- 受限冻结的 `docs/formal-crawl-contract.md`
- 受限冻结的 `docs/crawl-architecture.md`
- `config/frozen_files.json`

## 17. 测试方案

### 17.1 根项目单元测试

- 五个平台权威字段投影与顺序。
- 抖音封面、视频和音乐 URL 永远不产生 `ImageCandidate`。
- 知乎作者主页和头像不产生正文候选；公式图片继续排除。
- 小红书同一图片多个 URL 变体只产生一个候选。
- B站 HTTP/HTTPS、协议相对地址和变换 URL 得到相同稳定键。
- manifest 路径穿越、绝对路径、缺字段、重复序号和身份不一致被拒绝。
- HTML、JSON、视频、伪造扩展名、超限文件和解码炸弹被拒绝。
- SHA、MIME、尺寸和目标路径计算正确。
- 晋升重复执行幂等；已有路径 SHA 冲突时失败。
- upsert 在抖音签名查询参数变化后保留稳定键一致的本地元数据。
- upsert 在稳定资源键变化后清空旧本地关系并要求重新下载。
- 整帖一张失败时不提交任何该帖本地关系。

### 17.2 MediaCrawler 测试

- 微博 manifest 的 note ID、pid、顺序和文件一一对应。
- 抖音图文调用图片下载；视频候选和空图片列表都不调用 `get_aweme_video()`。
- 抖音图片失败不会写成功 manifest。
- 知乎搜索图片和详情补全图片使用同一正文列表。
- XHS 视频函数在图片模式下仍不可达。
- 所有 store 使用真实文件类型和原子写入。

### 17.3 执行器与数据库集成测试

- 通用正式 child 和 XHS 正式计划都要求图片本地化。
- `--get-media` 继续失败；正式模式缺少图片标志失败。
- `--no-import` 可生成 manifest，但不会晋升或写库。
- 临时 SQLite 中 `post_images_count`、正文图片行和非空 `local_path` 数量一致。
- 每个 `local_path` 都在项目长期目录内、文件存在、SHA 与数据库一致。
- `PRAGMA quick_check`、`foreign_key_check` 和唯一索引通过。
- 图片失败时 `persistence_verified` 不完成，发现 checkpoint 保持安全前沿。
- 重跑同一记录不增加主表、图片行或重复长期文件。

### 17.4 历史迁移测试

本节测试只允许在 I-14 `MAIN_PROGRAM_READY=true` 后进入；在此之前不得为了准备历史工具而提前
修改默认库或晋升现有文件。

- 使用默认库副本验证第 13.1 节五个平台的预期关系数。
- 原 `web_posts` 行数、正文文本、作者、指标、发布时间、关键词和发现表哈希不变。
- 小红书 17,150 个现有本地文件全部通过晋升并保持 SHA。
- 固定每平台 10 帖小样下载成功后，再进入分批全量。

## 18. 分步实施执行手册

### 18.1 总体阶段与不可跨越门禁

实施严格分成两个串行阶段：

```text
阶段 I：主程序实现
  -> MAIN_PROGRAM_READY 总验收
  -> 阶段 II：当前数据清理与补全
  -> HISTORICAL_DATA_COMPLETE 总验收
```

`MAIN_PROGRAM_READY` 未通过时，禁止开始任何 `H-*` 步骤。阶段 I 的数据库和文件测试只能使用：

- 测试 fixture；
- `temp/` 下的临时 SQLite；
- `temp/` 下的临时媒体根目录；
- `--no-import` 真实小样 staging；
- 默认库的只读查询或 SQLite backup 副本。

阶段 I 禁止：

- 对 `data/trippostcollect.sqlite` 执行图片关系修复或历史路径更新；
- 批量下载当前库的历史图片；
- 把现有 XHS 文件晋升到 `data/media/`；
- 运行带 `--apply` 的历史工具；
- 用“历史库已经补齐”代替主程序的新记录端到端验收。

每一步都遵守同一执行规则：

1. 开始前确认上一步验收通过并记录当前 Git HEAD。
2. 只修改本步骤列出的文件和直接关联测试；发现跨步骤依赖时先更新本文，不静默扩大范围。
3. 先运行步骤专属测试，再运行受影响的回归测试。
4. 验收失败时停止，不进入下一步，不用临时兼容或跳过门禁掩盖失败。
5. 验收通过后形成独立 Git commit；MediaCrawler 定向补丁在其嵌套仓库单独提交并记录 SHA。
6. 每步报告必须列出变更文件、测试命令、测试计数、关键产物和未解决事项。

### 18.2 阶段 I：主程序实现

#### I-00：冻结实现基线

前置条件：本文已经进入 Git，根项目与 MediaCrawler 当前提交可解析。

实施动作：

- 记录根项目 HEAD、MediaCrawler HEAD、Python/uv 版本和默认库 SHA-256。
- 只读记录五平台帖子数、当前图片关系数、本地路径数、数据库 `quick_check` 和磁盘可用空间。
- 建立主程序测试使用的临时根目录；确认它不指向默认库或 `data/media/`。
- 将当前 2026-08-07 数量保存为基线报告，但明确历史阶段会重新盘点。

验收命令：

```bash
git status --short
git rev-parse HEAD
git -C tools/MediaCrawler rev-parse HEAD
source .venv/bin/activate
python scripts/verify_frozen_files.py
```

验收标准：

- 两个仓库 HEAD 和工作区状态均被记录；不存在来源不明的修改。
- 默认库 `quick_check=ok`、外键违规为 0，并已记录 SHA-256。
- 临时根目录和默认库、长期媒体目录不是同一路径。
- 本步骤没有修改默认库、配置、浏览器状态或图片文件。

失败停止点：默认库不一致、工作区存在无法归属的重叠修改、MediaCrawler HEAD 不明确时停止。

产物：基线报告、根项目提交、MediaCrawler 提交和默认库摘要。

#### I-01：五平台显式正文图投影与头像过滤

前置条件：I-00 通过。

实施动作：

- 新增 `image_candidates.py` 和 `ImageCandidate`。
- 实现第 5 节五个平台白名单字段映射、顺序、URL 规范化和同帖去重。
- 明确让头像、作者主页、封面、视频、音乐、搜索预览和同图变体返回 0 个正文候选。
- 将 `row_for_record()` 与正式图片字段校验切换到新投影函数。
- 暂不下载、不创建长期目录、不修改默认库。

专属测试：

```bash
source .venv/bin/activate
python -m pytest tests/test_image_candidates.py
python -m ruff check \
  src/trippostcollect/artifacts/image_candidates.py \
  scripts/mediacrawler_crawl.py \
  tests/test_image_candidates.py
```

验收标准：

- B站、微博、XHS、抖音、知乎 fixture 的正文图片数量和顺序精确一致。
- `avatar_url`、`author_avatar`、`author_profile_url`、`cover_url`、
  `video_download_url`、`music_download_url` 全部不产生 `content` 候选。
- XHS 每个图片对象只产生一个候选；知乎公式图片为 0。
- 默认库只读投影得到当前基线：B站 18,050、微博 6,384、抖音 4,528、知乎 10,765、
  XHS 17,416；该检查不写库。
- `post_images_count` 来自新投影，而不是递归扫描。

失败停止点：任何头像/封面/视频/音乐进入候选，或任一平台正文顺序不稳定时停止。

产物：候选投影模块、fixture、投影计数报告和独立 commit。

#### I-02：稳定资源键与 manifest schema

前置条件：I-01 通过，所有候选均有平台、帖子 ID、来源顺序和 URL。

实施动作：

- 实现第 6 节各平台 `source_asset_key`。
- 定义 `image_manifest` schema v1、序列化、反序列化和逐帖完整性校验。
- 实现 manifest 中的路径、身份、重复序号、状态和敏感字段限制。
- 为抖音 `images[].uri`、微博 `pid` 和 XHS 路径身份增加 fixture。

专属测试：

```bash
source .venv/bin/activate
python -m pytest \
  tests/test_image_candidates.py \
  tests/test_image_manifest.py
python -m ruff check \
  src/trippostcollect/artifacts/image_candidates.py \
  src/trippostcollect/artifacts/image_manifest.py \
  tests/test_image_manifest.py
```

验收标准：

- 同一平台资产在协议、域名变体或签名查询参数变化后得到相同稳定键。
- 不同资产不得碰撞；fallback 使用确定性 SHA-256。
- manifest 拒绝绝对路径、`..`、重复 `(role, source_index)`、未知 schema、身份不匹配和成功字段
  不完整。
- manifest round-trip 不丢字段，输出排序稳定，可计算可复现 SHA-256。
- manifest/摘要中不存在 Cookie、Storage State 或请求头。

失败停止点：稳定键依赖完整签名 URL、Python 随机 hash 或存在已知碰撞时停止。

产物：稳定键实现、manifest schema/模型、测试和独立 commit。

#### I-03：文件安全、staging 与长期晋升组件

前置条件：I-02 通过。

实施动作：

- 在路径模块增加 `LOCAL_MEDIA_ROOT` 和 `IMAGE_MATERIALIZATION_RUNTIME`。
- 实现流式体积限制、魔数检测、PIL 验证、尺寸/像素限制、SHA-256 和真实扩展名。
- 实现 staging `.part`、`os.replace()`、长期内容哈希路径、`fsync` 和冲突拒绝。
- 从 `image_proxy.py` 抽取通用 URL/响应安全校验；保持管理端预览行为不变。
- 所有测试使用 pytest `tmp_path`，不得写 `data/media/`。

专属测试：

```bash
source .venv/bin/activate
python -m pytest \
  tests/test_image_materialization.py \
  apps/admin_api/tests/test_readonly_api.py
python -m ruff check \
  src/trippostcollect/artifacts/image_materialization.py \
  src/trippostcollect/artifacts/image_proxy.py \
  src/trippostcollect/core/paths.py
```

验收标准：

- JPEG、PNG、WebP、GIF、AVIF 正常通过并取得真实 MIME、尺寸和 SHA。
- SVG、HTML、JSON、音视频、伪造后缀、超限字节和超限像素全部拒绝。
- 路径穿越、绝对路径、重定向到受限地址全部拒绝。
- 相同文件重复晋升只产生一个目标；不同 SHA 不覆盖已有目标。
- 模拟异常中断只留下可识别 `.part`，不会产生被数据库当作成功文件的半文件。
- 管理端既有图片预览测试保持通过。

失败停止点：任何非图片字节可进入成功态、文件可逃出受控目录或冲突会覆盖已有文件时停止。

产物：纯本地物化组件、安全测试、临时文件报告和独立 commit。

#### I-04：SQLite 图片关系与 upsert

前置条件：I-01 至 I-03 通过。

实施动作：

- 扩展 `raw_image_json` 的来源顺序、稳定资源键和 `local_file` 证据。
- 修改 `upsert_web_post()`，按第 9.2 节顺序保护已验证本地元数据。
- 实现整帖图片关系的事务性提交；一张失败时整帖不提交。
- 实现 URL 更新但资产键不变、资产键变化、顺序变化和图片删除/增加路径。
- 只使用内存或 `temp/` SQLite 测试，不操作默认库。

专属测试：

```bash
source .venv/bin/activate
python -m pytest \
  tests/test_mediacrawler_import.py \
  tests/test_image_persistence.py
python -m ruff check \
  scripts/mediacrawler_crawl.py \
  tests/test_image_persistence.py
```

验收标准：

- `post_images_count = content rows = local rows`。
- `image_index` 在 `content` 角色内从 0 连续递增。
- 签名 URL 变化、稳定键不变时保留路径、SHA、尺寸和 MIME。
- 稳定键变化或本地文件验证失败时不复用旧关系。
- 整帖任一图片失败时，数据库保持事务前状态。
- 重跑不增加 `web_posts`、图片行或重复文件；外键和唯一索引通过。

失败停止点：出现半帖提交、签名变化丢路径、错误资产复用或主表重复时停止。

产物：upsert 改造、临时 SQLite 验证报告和独立 commit。

#### I-05：B站主程序图片适配

前置条件：I-04 通过。

实施动作：

- 在根项目 article 详情路径接入图片 staging 和 manifest。
- 只使用 `extract_bilibili_detail_images()` 的详情结果，禁止搜索预览图。
- 复用当前 article Cookie、Referer 和 User-Agent。
- 下载失败分类接入 B站安全前沿；不触发 MediaCrawler 视频代码。

专属测试：

```bash
source .venv/bin/activate
python -m pytest \
  tests/test_bilibili_article_detail.py \
  tests/test_bilibili_image_materialization.py \
  tests/test_bilibili_formal_route.py
```

验收标准：

- Opus 段落图、旧 article HTML 图和 fallback 详情图分别有 fixture 覆盖。
- 搜索 `image_urls` 不能单独产生下载任务。
- manifest 数量、顺序、资源键与详情正文图完全一致。
- 模拟图片 429/5xx/超时后当前页不推进、候选不入库、不进入已处理记忆。
- 测试和诊断输出没有任何视频请求或文件。

失败停止点：预览图混入、详情失败仍入库或视频路径可达时停止。

产物：B站适配、测试 manifest、失败安全前沿证据和独立 commit。

#### I-06：微博主程序图片适配

前置条件：I-05 通过，MediaCrawler 工作区干净。

实施动作：

- 改造 `get_note_images()`，传递微博 ID、`pid`、来源顺序和 URL。
- 复用 `wb_client.get_note_image()`，改为逐帖原子 staging 和真实格式。
- 只对初步字段有效图文下载；已知 ID 继续在媒体处理前跳过。
- 在 MediaCrawler 嵌套仓库提交定向补丁并记录 SHA。

专属测试：

```bash
cd tools/MediaCrawler
uv run pytest \
  tests/test_weibo_image_download.py \
  tests/test_weibo_store.py
```

根项目回归：

```bash
source .venv/bin/activate
python -m pytest \
  tests/test_image_manifest.py \
  tests/test_mediacrawler_import.py
```

验收标准：

- `mblog.pics` 的每个正文图与 note ID、pid、序号和 manifest 一一对应。
- URL 查询参数不进入文件扩展名，真实格式检测正确。
- 头像、作者主页及无图微博不产生正文图片文件。
- 已知 ID 不发生图片请求；失败图片不写成功 manifest。
- MediaCrawler 和根项目相关测试全部通过，嵌套仓库 commit 可复现。

失败停止点：平铺文件无法追溯到帖子、扩展名不可信或无效候选仍批量下载时停止。

产物：MediaCrawler 微博补丁 commit、根项目解析测试和 manifest 样例。

#### I-07：小红书主程序图片适配

前置条件：I-06 通过；本步骤只改新抓取路径，不迁移当前 17,150 个文件。

实施动作：

- 保留 `get_notice_media()` 图片专用和视频禁用语义。
- 每个图片对象只选一个权威 URL，写真实格式、原子 staging 和 manifest。
- 正式 runner 计划固定要求图片，但本步骤不运行默认库历史晋升。
- 在 MediaCrawler 嵌套仓库提交定向补丁并记录 SHA。

专属测试：

```bash
cd tools/MediaCrawler
uv run pytest \
  tests/test_xhs_image_download.py \
  tests/test_xhs_media_policy.py
```

根项目回归：

```bash
source .venv/bin/activate
python -m pytest \
  tests/test_xhs_pool.py \
  tests/test_xhs_discovery.py \
  tests/test_image_candidates.py
```

验收标准：

- 一个 XHS 图片对象只写一个文件和一行 manifest。
- 文件后缀与真实 MIME 一致，不再固定伪装为 `.jpg`。
- `get_notice_video()` 和视频 store 在图片模式测试中调用次数为 0。
- 当前默认库和现有 XHS 文件的 SHA、路径和行数在本步骤前后完全不变。
- 新抓取 fixture 和临时目录测试通过。

失败停止点：视频分支可达、同图变体重复下载或现有历史文件被改动时停止。

产物：MediaCrawler XHS 补丁 commit、新记录 manifest fixture 和默认库不变量报告。

#### I-08：知乎主程序图片适配

前置条件：I-07 通过。

实施动作：

- 为 Zhihu client 增加复用当前会话的图片请求。
- 新增知乎图片 store、原子 staging 和 manifest。
- 搜索有图和详情补全后的 `image_list` 共用同一下载入口。
- 保持公式、头像和作者主页排除。

专属测试：

```bash
cd tools/MediaCrawler
uv run pytest \
  tests/test_zhihu_image_download.py \
  tests/test_zhihu_detail_images.py
```

根项目回归：

```bash
source .venv/bin/activate
python -m pytest \
  tests/test_image_candidates.py \
  tests/test_mediacrawler_pagination.py
```

验收标准：

- 搜索正文图和详情补全正文图都产生准确 manifest。
- `/equation?`、`avatar_url`、`author_profile_url` 下载次数均为 0。
- `request_failed`/`parse_failed` 不能进入图片成功态。
- `detail_observed` 且正文图完整时才能通过图片门禁。
- 无视频文件、无 zvideo 图片任务。

失败停止点：详情未观察仍下载或公式/作者资源进入正文图时停止。

产物：MediaCrawler 知乎补丁 commit、两类详情 fixture 和 manifest 样例。

#### I-09：抖音严格 images-only 适配

前置条件：I-08 通过。抖音放在最后实现，因为当前 `get_aweme_media()` 存在视频回退。

实施动作：

- 新增严格图片入口：图片列表为空直接返回，永不调用 `get_aweme_video()`。
- 保留 `images[].uri` 等稳定资产元数据。
- 实现逐帖串行、原子 staging、真实格式和 manifest。
- 新记录只使用当前 `dy_client` 会话和新鲜签名 URL。
- 历史 URL 刷新逻辑不在本步骤实现，留到 H-07。

专属测试：

```bash
cd tools/MediaCrawler
uv run pytest \
  tests/test_douyin_image_only.py \
  tests/test_douyin_store.py
```

根项目回归：

```bash
source .venv/bin/activate
python -m pytest \
  tests/test_image_candidates.py \
  tests/test_mediacrawler_pagination.py \
  tests/test_discovery_checkpoints.py
```

验收标准：

- 图文候选只请求 `note` 图片；空图片列表、视频候选的图片/视频下载次数都为 0。
- `cover_url`、`video_download_url`、`music_download_url` 不出现在候选或 manifest。
- 测试以 spy 证明 `get_aweme_video()`、视频 store、音乐请求均未调用。
- 签名查询参数变化不改变 `source_asset_key`。
- 图片可恢复失败不推进 page/offset/search ID，也不写入已处理候选。

失败停止点：任何视频、音乐或封面字节请求可达时停止，不能靠上层过滤掩盖。

产物：MediaCrawler 抖音补丁 commit、无视频调用证据和 manifest fixture。

#### I-10：根执行器物化与导入编排

前置条件：I-05 至 I-09 五个平台适配全部通过。

实施动作：

- 按第 11.2 节接入 manifest 收集、根项目复验、长期晋升和 `MaterializedImage` 注入。
- 增加 `--media-root`，正式默认使用 `LOCAL_MEDIA_ROOT`；测试/诊断允许显式指定 `temp/` 子目录。
- 正式模式要求 `--download-images`；`--no-import` 可只生成 staging/manifest。
- 把图片本地化完成谓词并入 `collect_formal_records()` 和 `import_completion_met`。
- 暂不修改父 runner 完成阶段。

专属测试：

```bash
source .venv/bin/activate
python -m pytest \
  tests/test_image_materialization.py \
  tests/test_image_persistence.py \
  tests/test_mediacrawler_import.py \
  tests/test_mediacrawler_pagination.py
```

验收标准：

- 五平台 fixture 走同一根项目验证和晋升代码，没有第二套平台持久化逻辑。
- 正式模式少 manifest、少文件、数量不符、身份不符或 SHA 不符时不入库。
- `--no-import --download-images` 只产生 staging/manifest，不写长期目录和 SQLite。
- 临时 SQLite 与临时媒体根目录完成整帖事务，重复执行幂等。
- `image_materialization.complete` 与 `import_completion_met` 逻辑一致。

失败停止点：可绕过 manifest、先入库后补路径或诊断模式污染长期目录时停止。

产物：根执行器编排、临时端到端报告和独立 commit。

#### I-11：runner、冻结状态与完成谓词

前置条件：I-10 通过。

实施动作：

- 通用 runner 为四个平台正式 child 强制传 `--download-images` 和冻结的正式媒体根。
- XHS runner 固定 `local_image_storage_required=true`，移除目标配置可选性。
- 将 manifest 证据并入 `artifacts_verified`，将数据库/文件一致性并入 `persistence_verified`。
- 默认数量和来源耗尽模式均增加 `local_images_complete` 门禁。
- 失败后保持后续阶段 frozen，保留安全 checkpoint。

专属测试：

```bash
source .venv/bin/activate
python -m pytest \
  tests/test_bilibili_formal_route.py \
  tests/test_xhs_pool.py \
  tests/test_discovery_checkpoints.py \
  tests/test_image_runner_contract.py
```

验收标准：

- dry-run 冻结计划明确包含本地图片要求和媒体根，但不下载、不入库。
- 正式 child 命令全部使用项目 `--download-images`，没有开放 `--get-media`。
- manifest/文件失败使 `artifacts_verified` 或 `persistence_verified` 失败，`task_finalized` 保持 frozen。
- 图片完整时原有数量、来源耗尽、行为、策略、作者和分页门禁全部保持生效。
- XHS 配置旧 `download_images` 字段被删除且没有兼容分支。

失败停止点：任一 runner 可完成 URL-only 任务、冻结输入未包含图片契约或失败后仍 finalized 时停止。

产物：runner 变更、临时 dry-run 状态、测试报告和独立 commit。

#### I-12：五平台主程序综合回归

前置条件：I-11 通过，五个平台单元和平台适配测试均通过。

实施动作：

- 运行根项目全量测试、Ruff、编译和冻结文件验证。
- 运行 MediaCrawler 全量非外部依赖测试；Redis 等环境依赖必须单独标明，不能把代码失败归为环境。
- 使用 fixture 执行五平台临时 SQLite + 临时媒体根端到端测试。
- 对每个平台执行不超过 10 帖的 `--no-import --download-images` 真实小样；只验证 staging 和
  manifest，不触碰默认库。
- 对 B站、微博、知乎、抖音使用通用持久登录态；XHS 严格使用独立账号工作流。真实小样不是正式
  数量任务，不提交发现 checkpoint。

通用验证命令基线：

```bash
source .venv/bin/activate
python -m pytest
python -m ruff check \
  src \
  scripts \
  tests \
  apps/admin_api
python -m compileall -q \
  src \
  scripts \
  tests
python scripts/verify_frozen_files.py
```

MediaCrawler 验证：

```bash
cd tools/MediaCrawler
uv run pytest
```

验收标准：

- 根项目全量测试 0 失败；静态检查、编译、冻结校验全部通过。
- MediaCrawler 与图片有关测试 0 失败；外部服务跳过项有明确清单。
- 五平台临时端到端均满足第 19.1 节关系等式。
- 五平台真实小样 manifest 的候选数、文件数、成功数一致，头像/封面/视频/音乐请求为 0。
- 默认库 SHA、行数、图片关系和现有本地路径与 I-00 基线完全一致。

失败停止点：任一平台只能靠 mock 通过、真实小样字段身份不一致或默认数据发生变化时停止。

产物：全量测试摘要、五平台小样摘要、默认库前后不变量报告。

#### I-13：治理文档、运维文档与冻结哈希同步

前置条件：I-12 代码和综合回归通过，接口和完成语义已经稳定。

实施动作：

- 按第 16.3 节更新正式契约、架构、持久化、字段覆盖和五个平台文档。
- 更新运维命令、失败分类、磁盘预检、备份和报告读取顺序。
- 对受限冻结文档执行明确解冻、更新哈希、重新设为不可变并验证。
- 文档中的 CLI、字段、文件路径、错误码和摘要必须与实际代码逐项核对。

验收命令：

```bash
source .venv/bin/activate
python scripts/verify_frozen_files.py
git diff --check
```

验收标准：

- 所有本地 Markdown 链接有效。
- 代码中每个公开参数、摘要字段、错误码和正式完成谓词均在文档出现。
- 文档不再声称正式任务只保存 URL，也不保留 XHS 可选图片开关。
- 冻结文件校验通过，配置哈希与正文一致。
- 本步骤仍未修改默认库或执行历史补全。

失败停止点：代码/文档语义不一致、冻结校验失败或需要保留未说明兼容层时停止。

产物：治理文档 commit、冻结哈希验证和文档链接报告。

#### I-14：`MAIN_PROGRAM_READY` 总验收

前置条件：I-00 至 I-13 全部通过，无跳过的功能步骤。

实施动作：

- 重新执行 I-12 全量验证。
- 汇总根项目与 MediaCrawler commit 序列，确认每步可追溯。
- 复核五平台新记录路径、URL-only 不可降级、头像过滤和视频禁用。
- 复核默认库及现有图片文件从 I-00 起没有被历史操作修改。
- 生成 `MAIN_PROGRAM_READY` 验收报告和实现版本号。

验收标准：

- 五个平台都已实现：显式投影、平台会话下载、manifest、根校验、长期晋升、SQLite 写回。
- 五个平台临时端到端和真实 `--no-import` 小样全部通过。
- 正式 runner 五阶段已经纳入本地图片完整性，任何平台不能 URL-only 完成。
- 头像、作者主页、封面、视频、音乐和公式图片过滤有单元与平台调用证据。
- 根项目和 MediaCrawler 全量相关测试、Ruff、编译、冻结校验全部通过。
- 默认库 SHA/行数/关系与 I-00 一致；没有执行 `materialize_local_images.py --apply`。
- 验收报告明确写出 `MAIN_PROGRAM_READY=true`。

失败停止点：任何一项不满足都保持 `MAIN_PROGRAM_READY=false`，禁止进入 H-00。

产物：主程序发布 commit/tag、验收报告、完整测试摘要。到这里主程序开发完成，但整个历史数据
补全工程尚未完成。

### 18.3 阶段 II：当前数据清理与补全

#### H-00：重新盘点并冻结历史输入

前置条件：I-14 报告存在且 `MAIN_PROGRAM_READY=true`。

实施动作：

- 重新读取当前默认库，而不是沿用 2026-08-07 固定数量。
- 用已经验收的显式投影计算每平台权威正文图、现有本地关系、误分类、重复变体和缺口。
- 记录默认库 SHA、`quick_check`、外键、磁盘空间和当前浏览器/账号可用性。
- 冻结历史 campaign ID、输入数据库 SHA、主程序版本和 MediaCrawler 版本。

验收标准：

- 实时盘点可逐平台复算，权威图总数等于各帖子投影之和。
- 输入数据库 SHA、主程序 commit、MediaCrawler commit 和 campaign ID 已冻结。
- `quick_check=ok`、外键违规为 0。
- 报告只读生成，默认库和图片文件没有变化。

失败停止点：数据库不一致、主程序版本与 I-14 不同或磁盘容量无法估算时停止。

产物：历史输入冻结报告。本文早期数字仅作为差异参考，从本步骤开始以该报告为准。

#### H-01：历史工具实现与数据库副本演练

前置条件：H-00 通过。历史工具直到此步骤才开始实现。

实施动作：

- 实现 `materialize_local_images.py` 的 dry-run、平台选择、批次、恢复、报告、备份和 `--apply`。
- 实现默认 dry-run 的 `gc_local_images.py`，但不执行删除。
- 在默认库 SQLite backup 副本上执行第 13.1 节关系清理和投影重建。
- 复用已经验收的主程序候选、下载、manifest、晋升和 upsert 组件，不复制第二套逻辑。

专属测试：

```bash
source .venv/bin/activate
python -m pytest \
  tests/test_historical_image_materialization.py \
  tests/test_local_image_backfill.py
```

验收标准：

- 不带 `--apply` 时数据库和文件 SHA 均不变化。
- `--apply` 必须先创建可打开、`quick_check=ok` 的 SQLite backup。
- 数据库副本关系清理结果与 H-00 权威投影精确一致。
- `web_posts` 及非图片字段逐条哈希不变，发现 checkpoint/seen candidates 完全不变。
- 工具中没有平台专用第二套投影或下载实现。

失败停止点：dry-run 有写入、副本演练改变主表内容或工具绕过主程序组件时停止。

产物：历史工具 commit、副本演练报告和测试报告。

#### H-02：默认库备份、容量与执行计划门禁

前置条件：H-01 通过。

实施动作：

- 创建默认库一致性 backup，并验证 backup SHA、`quick_check` 和可恢复性。
- 以每平台小样估算 p50/p95 图片大小、staging 峰值和长期空间。
- 确认磁盘可用空间至少覆盖估算总量 2 倍和安全余量。
- 冻结平台执行顺序、批次大小、最大重试、超时、报告目录和操作人停止条件。

验收标准：

- backup 可独立打开，行数、关系数和输入库一致。
- 容量报告覆盖 staging、长期文件、backup 和日志；空间满足门槛。
- 执行计划固定为 XHS、B站、微博、知乎、抖音；抖音最后。
- 本步骤没有关系重建或批量补下载。

失败停止点：backup 无法恢复、空间不足或批次/停止条件未冻结时停止。

产物：默认库 backup、容量报告和冻结执行计划。

#### H-02A：脱离模型的后台执行编排

目标：H-04 至 H-08 不依赖模型逐批轮询。后台 worker 只执行本文已经冻结的确定性步骤，不自行
改变投影、批次、重试、排除清单或完成口径。

实施动作：

- `historical_image_worker.py start` 使用脱离终端会话的子进程启动，持有 campaign 级 `flock`
  单实例锁；启动时若仍有独立单批进程则拒绝并发接管。
- 每轮重新读取 SQLite 缺口，只选择固定顺序中第一个未完成平台。平台本地关系为 0 时先跑固定
  10 帖，成功后才切换 H-02 冻结扩大批次；每个子批次仍由 `historical_platform_images.py`
  创建独立 backup、staging、manifest 和事务报告。
- 每个平台开始前自动运行登录 warmup 的一秒人工等待预检。有效 profile 会自动通过；失效时
  worker 写 `auth_required` 并退出，不打开无边界等待，也不继续下载。操作人完成
  `login_warmup.py` 后再次 `start` 即从数据库安全前沿恢复。
- worker 固定记录 git commit 和关键执行文件 SHA；运行期间任一代码或冻结门禁漂移时写
  `code_drift` 并停止。每批前按剩余缺口重新计算 H-02 p95、两倍余量和 10 GiB 安全空间。
- `stop` 只写停止请求；worker 完成当前整批事务后停止，不向正在下载或提交的子进程发送信号。
- B站使用详情图和文章 Referer；微博使用 `mblog.pics` 投影和大图代理；知乎使用最终正文
  `image_list`；抖音先直取旧签名，失败后按 `aweme_id` 只刷新一次图片详情，单图直取加刷新总尝试
  不超过 3，视频、音乐和封面请求计数固定为 0。
- 平台缺口归零后，`validate_historical_images.py` 对该平台逐文件重算 SHA、MIME、宽高、路径、
  关系身份、孤儿和 H-00 不变量，通过后才进入下一平台。五平台归零后再次全库验收并运行
  `gc_local_images.py` dry-run；不会自动删除文件。

控制入口：

```bash
source .venv/bin/activate
python scripts/historical_image_worker.py start
python scripts/historical_image_worker.py status
python scripts/historical_image_worker.py stop
```

状态与日志固定为：

```text
data/runtime/image_materialization/historical-images-20260807-v1/background-worker/
  state.json
  worker.log
  worker.pid                 # 只在进程存活时存在
  worker.lock
  stop.requested             # 仅收到安全停止请求时存在
  validation/<platform>/
  validation/all/
```

验收标准：

- 重复 `start` 不产生第二 worker；独立单批尚在运行时拒绝接管。
- 终端和模型会话结束后 PID 仍存活，`state.json` 的批次、报告和库存持续推进。
- 子批失败时数据库停在前一个完成批次；状态明确区分 `auth_required`、`capacity_blocked`、
  `code_drift`、`validation_failed` 和普通 `failed`。
- 重启 worker 不依赖人工游标，直接按 SQLite `local_gap` 选择剩余帖子，不重复已具有完整本地关系
  的帖子。
- 只有全库验收通过并生成 GC dry-run 报告后，状态才允许写
  `historical_data_complete=true` 和 `status=completed`。

失败停止点：锁冲突、代码漂移、空间不足、登录失效、任一图片/manifest/事务失败或平台验收失败
均立即停止；后台化不改变 H-02 的任何失败门禁。

产物：后台 worker、四平台单批适配、抖音图片详情刷新器、平台/全库验收器、状态文件和操作手册。

#### H-03：小红书现有文件晋升与缺口补齐

状态：已完成。平台级结果为权威正文图、`content` 行、本地路径和长期文件均 17,416，缺口、
误分类、孤儿、缺失文件和头像本地路径均为 0；完整证据见
[`2026-08-07-historical-image-h03-xhs-acceptance.md`](2026-08-07-historical-image-h03-xhs-acceptance.md)。

前置条件：H-02 通过，XHS 账号状态和租约满足平台文档。

实施动作：

- 先 dry-run 映射现有已验证 XHS 文件到长期目录。
- 按 SHA 复用现有 17,150 个基线文件；实际数量以 H-00 为准。
- 只对缺失图片重新取得笔记详情并下载；不重新下载已验证文件。
- 先固定 10 帖 apply，再按冻结批次扩大。

验收标准：

- 晋升前后每个复用文件 SHA 完全一致。
- XHS 权威正文图、`content` 行、本地路径和文件数相等。
- 同图 CDN 变体为 0，作者头像本地路径仍为空。
- 视频请求和视频文件新增为 0。
- 每批 `quick_check=ok`、外键为 0、待重试按报告可恢复；进入下一平台前待重试必须为 0 或有用户
  逐项批准的排除清单。

失败停止点：已有文件 SHA 变化、误下载头像/视频或任一帖子部分提交时停止并从 backup 恢复。

产物：XHS 分批报告、数据库 backup 引用、长期路径和平台完成报告。

#### H-04：B站历史补全

前置条件：H-03 平台完成报告通过。

实施动作：

- 以 H-00 B站权威详情图片为输入，不重新跑关键词发现。
- 先尝试现有 URL；需要会话时复用 B站持久登录态和文章 Referer。
- 固定 10 帖 apply 通过后按批次扩大。
- 批次 009 遇到 112,490,000 像素的有效原始 JPEG 后，按 8.3 节治理记录将归档像素上限调整为
  150,000,000；失败批次数据库哈希前后相等，必须在变更测试通过并提交后整批重跑。

验收标准：

- B站权威正文图、数据库 `content` 行、本地路径和文件数相等。
- 搜索预览图、视频和作者头像下载数为 0。
- 正文、作者、指标、关键词、发布时间和发现记忆哈希不变。
- 所有文件 SHA、MIME、尺寸和路径复验通过；待重试归零或逐项批准排除。

失败停止点：详情预览口径混入、登录失效仍继续或非图片字段变化时停止。

产物：B站分批报告和平台完成报告。

#### H-05：微博历史补全

前置条件：H-04 平台完成报告通过。

实施动作：

- 使用 H-00 `image_list`/`mblog.pics` 权威关系和微博会话。
- 固定 10 帖 apply 后按批次扩大。
- `pid` 相同且 SHA 已存在时按主程序幂等规则复用。

验收标准：

- 微博权威正文图、数据库关系、本地路径和文件数相等。
- 每个文件可追溯到微博 ID、pid 和来源顺序。
- 头像和无图微博不产生文件；非图片字段和发现记忆不变。
- 待重试归零或有逐项批准排除，数据库完整性通过。

失败停止点：文件无法追溯到帖子、pid 冲突或无效候选被下载时停止。

产物：微博分批报告和平台完成报告。

#### H-06：知乎历史补全

前置条件：H-05 平台完成报告通过。

实施动作：

- 使用 H-00 正文 `image_list`；缺图只走已经验收的 answer/article 详情路径。
- 固定 10 帖 apply 后按批次扩大。
- 继续执行公式、头像和作者主页排除。

验收标准：

- 知乎权威正文图、数据库关系、本地路径和文件数相等。
- `/equation?`、作者头像、作者主页和 zvideo 文件新增均为 0。
- 详情状态与图片来源一致；非图片字段和发现记忆不变。
- 待重试归零或有逐项批准排除，数据库完整性通过。

失败停止点：公式/作者资源混入、详情未观察仍提交或内容类型越界时停止。

产物：知乎分批报告和平台完成报告。

#### H-07：抖音历史补全

前置条件：H-06 平台完成报告通过；抖音图片专用不变量已经在 I-09/I-14 验收。

实施动作：

- 先修复当前 `content` 误分类，只保留 H-00 `note_download_url/images[]` 权威关系。
- 旧签名 URL 先直取；失败时按 `aweme_id` 刷新一次详情图片资产，再下载。
- 不重新跑关键词发现，不调用视频、音乐或封面请求。
- 固定 10 帖 apply，覆盖直取成功、详情刷新成功和可恢复失败三类后再扩大。

验收标准：

- 抖音权威正文图、数据库关系、本地路径和文件数相等。
- 封面、视频 URL、音乐 URL 误分类均为 0。
- 日志和 spy/计数证明视频、音乐请求及文件新增为 0。
- 详情刷新只更新图片 URL/资产证据，不改变发现 checkpoint 或其他帖子字段。
- 待重试归零或有用户逐项批准的排除清单，数据库完整性通过。

失败停止点：视频/音乐/封面路径可达、签名失败被误判永久无效或刷新推进发现记忆时停止。

产物：抖音三类路径报告、分批报告和平台完成报告。

#### H-08：`HISTORICAL_DATA_COMPLETE` 全库验收

前置条件：H-03 至 H-07 五个平台完成报告全部通过。

实施动作：

- 运行第 19.2 节 SQL 和逐文件 SHA/MIME/尺寸/路径校验。
- 对比 H-00 输入冻结报告，验证非图片字段、主表数量和发现记忆不变量。
- 审计无引用文件、`.part`、视频/音乐目录新增和磁盘余量；GC 只 dry-run。
- 汇总所有 backup、批次报告、排除清单和平台完成报告。

验收标准：

- 五个平台 `relation_mismatches=0`、`local_path_mismatches=0`。
- 所有非空本地路径文件存在且 SHA、MIME、尺寸一致；路径越界和冲突为 0。
- 头像、作者主页、封面、视频、音乐和公式图片不在正式正文本地集合。
- `quick_check=ok`、外键违规为 0；主表及非图片字段不变量通过。
- 待重试为 0，或每个排除项都有用户逐项批准且不计成功。
- 最终报告明确写出 `HISTORICAL_DATA_COMPLETE=true`。

失败停止点：任何 mismatch、缺失文件、未经批准排除或发现记忆变化都不能宣布完成。

产物：全库验收报告、最终数据库 SHA、长期媒体清单 SHA、backup 索引和工程完成 commit。

## 19. 验收标准

### 19.1 新抓取验收

每个正式有效帖子必须满足：

```text
post_images_count
= content image rows
= content rows with non-empty local_path
= manifest downloaded rows
```

并且：

- 每个本地路径都位于 `data/media/<platform>/`。
- 每个文件存在，数据库 SHA、MIME、宽高与重新检测一致。
- `raw_image_json.source_asset_key` 非空且同帖唯一。
- `image_index` 为正文图从 0 开始的连续序列。
- 摘要 `image_materialization.complete=true`。
- 冻结状态五阶段全部完成，`persistence_verified` 包含图片计数和校验摘要。
- `data/media/**/videos`、MediaCrawler `videos/` 和音乐目录没有本轮新增文件。

### 19.2 历史全量验收

- 权威正文图总数以 H-00 实时重算为准；2026-08-07 当前基线为 57,143。
- 每个权威正文图片行都有本地路径；2026-08-07 当前基线缺口为 39,993，最终以 H-00 为准并降为 0。
- 作者头像仍为可选远程 URL，不因未本地化导致失败。
- `web_posts` 总行数不因补下载变化。
- 抖音非正文媒体误分类为 0，知乎作者主页误分类为 0，小红书变体重复为 0。
- 全库本地路径无越界、无缺失文件、无 SHA 冲突、无重复 `(post, role, index)`。
- `PRAGMA quick_check='ok'` 且 `PRAGMA foreign_key_check` 返回 0 行。
- 历史报告包含数据库备份、输入/输出计数、映射 SHA 和失败分类；待重试为 0，或存在用户逐项批准
  的独立排除清单。排除项不得伪装成本地化成功。

最低验收 SQL：

```sql
WITH content_images AS (
  SELECT
    p.platform_key,
    p.id AS web_post_id,
    p.post_images_count,
    COUNT(i.id) AS image_rows,
    SUM(CASE WHEN i.local_path IS NOT NULL AND i.local_path <> '' THEN 1 ELSE 0 END) AS local_rows
  FROM web_posts p
  LEFT JOIN web_post_images i
    ON i.web_post_id = p.id
   AND i.image_role = 'content'
  GROUP BY p.id
)
SELECT
  platform_key,
  COUNT(*) AS posts,
  SUM(post_images_count <> image_rows) AS relation_mismatches,
  SUM(image_rows <> local_rows) AS local_path_mismatches
FROM content_images
GROUP BY platform_key
ORDER BY platform_key;
```

正式验收要求五个平台的两个 mismatch 均为 0；SQL 之后仍必须逐文件校验 SHA，不能只看非空路径。

## 20. 回滚与故障处理

- 上线前保留代码提交点、默认库 SQLite backup、manifest 和文件晋升报告。
- 若某平台图片实现不稳定，暂停该平台正式 job；不得回退为 URL-only 并继续宣称正式完成。
- 数据库回滚使用上线前 SQLite backup；已经晋升的不可变文件可以保留为无引用文件，待独立 GC。
- 不删除已验证的小红书现有文件或路径，除非已确认新的长期副本存在且 SHA 一致。
- MediaCrawler 补丁回滚时同时回滚根项目对该平台图片模式的启用，防止重新进入视频回退路径。
- 图片失败排查顺序固定为 manifest 错误码、文件校验摘要、平台日志尾部、登录态和短小样；不全文
  展开 JSONL 或批量响应。

## 21. 完成定义

工程有两个不能混淆的完成状态。

### 21.1 主程序完成：`MAIN_PROGRAM_READY`

只有以下事项全部完成，才允许从阶段 I 进入历史数据阶段：

1. I-00 至 I-13 逐步验收全部通过，没有被跳过或事后补签的步骤。
2. 五平台显式正文图投影替换通用递归提取，头像等非正文资源被自动过滤。
3. 五平台安全图片下载、manifest、根项目校验、长期晋升和 SQLite 写回全部实现。
4. 正式 runner 把本地图片完整性纳入现有冻结完成门禁，URL-only 无法完成。
5. 单元、MediaCrawler、执行器、临时 SQLite、临时媒体目录和每平台真实小样全部通过。
6. 视频、音乐和作者头像未进入正文图片本地化路径。
7. 受限治理文档、平台文档、数据持久化文档和冻结哈希与代码一致。
8. 默认库和现有历史文件从 I-00 起保持不变。
9. I-14 报告明确记录 `MAIN_PROGRAM_READY=true`。

达到这里表示主程序可以正确处理以后新抓的图文，但不表示当前数据库历史图片已经补齐。

### 21.2 整体工程完成：`HISTORICAL_DATA_COMPLETE`

只有 `MAIN_PROGRAM_READY=true` 后继续完成以下事项，整个工程才算完成：

1. H-00 重新盘点并冻结当前数据，而不是直接使用本文早期容量数字。
2. H-01 至 H-07 按顺序完成工具、副本演练、备份和五平台历史处理。
3. 历史误分类修复完成，已有小红书文件无损晋升，历史缺口补齐或存在用户逐项批准的排除项。
4. 默认库、长期文件目录、运行报告和数据库备份通过第 19.2 节全量审计。
5. H-08 报告明确记录 `HISTORICAL_DATA_COMPLETE=true`。

不得把主程序完成提前汇报为历史数据完成，也不得为了补历史数据而跳过主程序总验收。
