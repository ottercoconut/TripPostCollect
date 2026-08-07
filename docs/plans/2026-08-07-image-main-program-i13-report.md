# I-13 治理文档、运维文档与冻结哈希报告

> 执行日期：2026-08-07（Asia/Shanghai）  
> 工程步骤：I-13  
> 结论：通过；I-14 总验收前仍禁止进入任何 H 历史补全步骤。

## 1. 本步骤范围

本步骤只把 I-01 至 I-12 已实现并验证的五平台正文图片本地化行为写入治理和操作文档，不改抓取
代码、不运行历史补全、不写默认内容数据。更新范围与
[工程实现方案](2026-08-07-multiplatform-local-image-storage.md) 第 16.3、I-13 节一致：

- [文档入口](../README.md)、[正式抓取执行契约](../formal-crawl-contract.md)、
  [抓取架构](../crawl-architecture.md)、[入库与校验](../data-persistence.md)；
- [平台字段覆盖](../platform-field-coverage.md)；
- [B站](../platforms/bilibili.md)、[微博](../platforms/weibo.md)、
  [小红书](../platforms/xhs.md)、[抖音](../platforms/douyin.md)、
  [知乎](../platforms/zhihu.md)；
- [正式抓取运行手册](../operations-runbook.md) 和 `config/frozen_files.json`。

## 2. 已固化的公开契约

### 2.1 CLI 与运行模式

- 正式通用 runner 和 XHS runner 固定注入 `--download-images` 与正式 `--media-root`；冻结计划固定
  `local_image_storage_required=true`。
- `--download-images` 只代表权威正文图，视频、音乐仍禁用；`--get-media` 继续直接失败。
- `--media-root` 默认是 `data/media`；显式覆盖仅允许项目 `temp/` 子目录。
- `--no-import --download-images` 只生成 staging/manifest 和根字节复验，
  `promotion_required=false`，不写长期目录、SQLite 或 checkpoint。
- 正式模式缺少 `--download-images` 在平台访问前失败；不存在 URL-only 降级或 XHS target 图片开关。

### 2.2 五平台正文图来源与噪声过滤

| 平台 | 权威字段 | 稳定身份 | 下载前排除 |
|---|---|---|---|
| B站 | 详情观察后的 `image_urls` | BFS 逻辑路径/规范 URL 哈希 | 搜索预览、封面、头像、视频 |
| 微博 | `mblog.pics` → `image_list` | `pid`，缺失时规范 URL 哈希 | 用户头像、封面、视频缩略图 |
| 小红书 | 详情 `image_list`，每对象择一 URL | notes 稳定路径 | 头像、作者主页、封面、视频 |
| 抖音 | `note_download_url` | `images[].uri`，缺失时规范 URL | 封面、视频、音乐、头像 |
| 知乎 | 正文/详情 `image_list` | 去变换后缀的 zhimg 路径哈希 | 公式、头像、作者主页、封面、zvideo |

被排除资源不产生正文候选、下载、manifest、长期文件或 `content` 图片关系。作者头像仍可保留
`author_avatar` URL-only 参考，但不下载、不计入 `post_images_count` 或本地正文图完整性；这既
满足自动忽略无用图片的要求，也不破坏已有作者展示字段。

### 2.3 统一摘要、文件和持久化

文档已逐项列出 `image_materialization` 的公开字段：`required`、`promotion_required`、
`candidate_posts`、`complete_posts`、`expected_images`、`downloaded_images`、
`validated_images`、`reused_images`、`promoted_images`、`retryable_failures`、
`terminal_failures`、`complete`、`manifest_paths`、`manifest_sha256`、`manifest_evidence` 和
`failures`。同时固化：

- staging 与 schema v1 `image_manifest.jsonl`；
- 根项目身份、路径、SHA、MIME、后缀、尺寸和解码复验；
- `data/media/<platform>/<post>/<index>-<asset_hash>.<real_ext>` 内容寻址晋升；
- `web_posts` 与 `web_post_images` 同一 SQLite savepoint；
- 正文图片的 `image_url/image_role/image_index/local_path/width/height/mime_type/sha256` 和
  `raw_image_json.local_file` 证据；
- `artifacts_verified` 与 `persistence_verified` 的文件/数据库职责边界。

正式等式为：

```text
candidate_posts == complete_posts
expected_images == downloaded_images == validated_images
promoted_images + reused_images == expected_images
retryable_failures == terminal_failures == 0
failures == []
```

并且 `formal_validation.image_materialization_complete=true`、
`local_images_complete=true`、`local_image_failure_count=0`，与原数量/来源耗尽、字段、行为、策略、
分页和真实入库谓词同时成立。

### 2.4 错误、恢复和运维

正式契约与运行手册覆盖 manifest 缺失/计数/身份/元数据错误、路径逃逸、文件缺失、非图片响应、
解码失败、超限、哈希不符、staging 冲突、晋升冲突、统一摘要缺失/不完整、平台下载失败和
`persistence_verified` 失败。运维顺序固定为：顶层摘要 → execution state → child summary →
manifest 证据 → SQLite/长期文件 → 必要时日志尾部。

运行手册新增 `df/du` 磁盘预检，以及历史补全、批量修复、清理和人工 SQL 前的 SQLite `.backup`
与 SHA-256 记录。普通新抓使用事务与幂等晋升；历史补全必须等待 I-14 明确
`MAIN_PROGRAM_READY=true`。

## 3. 冻结治理

三份自声明受限治理基线在内容定稿后加入 `config/frozen_files.json`，重新计算 SHA-256 并设置
macOS `uchg`：

| 文件 | SHA-256 | 不可变 |
|---|---|---|
| `docs/formal-crawl-contract.md` | `f39e42876dbe63c7fec046868fc04d6c907b219b9981246bfd0ae434167d17a5` | 是 |
| `docs/crawl-architecture.md` | `50b7251d10f3e9c9a363324b73a6c973d5a35d4174532c94c7135608f44ce6ea` | 是 |
| `docs/data-persistence.md` | `87426cc9b1f633f45cd970e22f46a0af74a7b1fc4d0730cf25fa6922b58eb6e0` | 是 |

以后修改这些文件必须获得明确用户授权，先移除不可变标志，完成代码/测试/关联文档同步后再更新
哈希并重新冻结。

## 4. 验收结果

| 检查 | 结果 |
|---|---|
| 本地 Markdown 链接 | `markdown_link_errors=0` |
| `git diff --check` | 通过 |
| `python scripts/verify_frozen_files.py` | `Frozen file verification passed.` |
| 三份治理文档 `uchg` | 全部存在 |
| 默认库 SHA-256 | `a7f78d3636025d61b442b50050afb2c36b7808772131d77df1c344bcdf371006`，与 I-00 一致 |
| 默认库大小 | `143273984 bytes`，与 I-00 一致 |
| `web_posts` / `web_post_images` | `6719 / 63477`，与 I-00 一致 |
| 非空 `local_path` 行 | `17150`，与 I-00 一致 |
| `PRAGMA quick_check` | `ok` |
| `data/media` 文件数 | `0`，与 I-00 一致 |
| 历史补全命令 | 未执行 |

## 5. 步骤结论

I-13 验收通过。治理、平台、持久化和运维文档与当前代码的五平台本地正文图片行为一致；头像等
非正文资源的“自动忽略下载”和可选 URL 参考关系已明确区分。下一步只能执行 I-14
`MAIN_PROGRAM_READY` 总验收，仍不得开始 H-00 或任何当前数据补全。
