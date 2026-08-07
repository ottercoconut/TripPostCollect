# 历史图片 H-01 工具与副本演练报告

> 结论：H-01 通过，允许进入 H-02；`HISTORICAL_DATA_COMPLETE=false`。
>
> campaign ID：`historical-images-20260807-v1`
>
> 输入基线：
> [`2026-08-07-historical-image-h00-input-freeze.json`](2026-08-07-historical-image-h00-input-freeze.json)

## 1. 本步骤完成内容

- 新增默认 dry-run 的 `scripts/materialize_local_images.py`，支持 H-00 campaign 校验、平台选择、
  受控批次、JSON resume state、JSON 报告、SQLite backup 和显式 `--apply`。
- 新增默认 dry-run 的 `scripts/gc_local_images.py`。它只扫描 `data/media` 与数据库引用差集；删除必须
  同时传入 `--apply` 和与 campaign ID 完全一致的 `--confirm-delete`，删除前还会创建 SQLite backup
  并重新读取引用集合。本步骤没有运行删除模式。
- 把主程序已验收的图片关系规范化、本地文件复验、稳定资源匹配和 SQLite 行替换抽到
  `trippostcollect.artifacts.image_persistence`。主程序与历史工具现在调用同一实现。
- 新增 `trippostcollect.artifacts.historical_image_materialization`，只负责历史 campaign、批次、哈希
  不变量、backup 和事务编排；五个平台投影仍唯一来自 `content_image_candidates()`。
- 新增 H-01 专属测试，覆盖默认 dry-run、backup 后 apply、副本关系重建、保护字段、批次恢复和
  GC 显式确认门禁。

## 2. 命令与安全边界

默认库全量只读预演命令：

```bash
source .venv/bin/activate
python scripts/materialize_local_images.py \
  --platform all \
  --batch-size 0 \
  --report <report.json>
```

该命令会写指定报告，但不会修改数据库或图片。没有匹配的 resume state 时，数据库 SHA、H-00
逐平台数量、`web_posts` 非图片字段哈希和四张发现记忆表哈希必须全部与冻结输入一致，否则在
规划前停止。

副本演练使用以下形态；H-01 没有把 `--apply` 指向默认库：

```bash
source .venv/bin/activate
python scripts/materialize_local_images.py \
  --db <rehearsal.sqlite> \
  --campaign docs/plans/2026-08-07-historical-image-h00-input-freeze.json \
  --platform all \
  --batch-size 0 \
  --resume-state <resume.json> \
  --report <report.json> \
  --backup-dir <backup-dir> \
  --apply
```

每次 apply 都先创建独立 SQLite backup，验证 `quick_check=ok` 和外键违规为 0，再以
`BEGIN IMMEDIATE` 执行当前批次。计划输入在事务开始时会重新计算摘要，变化即停止；成功后 resume
state 记录数据库输出 SHA 和每个平台最后提交的 `web_posts.id`。下一批只有在数据库 SHA 与 state
一致时才能继续。

## 3. 默认库全量 dry-run

| 项目 | 结果 |
|---|---:|
| 计划帖子 | 6,719 |
| 权威正文图片 | 57,143 |
| 待移除误分类 | 4,089 |
| 待合并重复变体 | 0 |
| 待新增缺失关系 | 0 |
| URL 规范化 | 0 |
| 可保留本地关系 | 17,150 |
| 计划 source digest | `3c536fb0ce43c08be1f42209d423859ef91e3157e257dbaad13090910886eb8b` |
| 计划 digest | `08deab2543eff67a863ea6ec67ffbccbb97bdc97d34c46d6a0e6c8bf997c47df` |

dry-run 前后默认库 SHA-256 均为
`a7f78d3636025d61b442b50050afb2c36b7808772131d77df1c344bcdf371006`。规划过程逐一复验已有
17,150 个本地关系的文件、SHA、MIME 和尺寸；没有文件被移动、晋升或改写。

## 4. SQLite 副本 apply 演练

最终代码从默认库逐字节副本开始执行。自动创建的演练 backup SHA-256 为
`78c6a4f352be0eb7585a1f82339824947de1188e733c8168889576f226c73149`，可独立打开，保留修复前
6,719 帖、61,232 条正文关系和 17,150 条本地关系，`quick_check=ok`、外键违规为 0。

| 平台 | 修复前正文关系 | 修复后正文关系 | 移除误分类 | 保留本地关系 |
|---|---:|---:|---:|---:|
| 小红书 | 21,040 | 17,416 | 3,624 | 17,150 |
| B站 | 18,050 | 18,050 | 0 | 0 |
| 微博 | 6,384 | 6,384 | 0 | 0 |
| 知乎 | 10,767 | 10,765 | 2 | 0 |
| 抖音 | 4,991 | 4,528 | 463 | 0 |
| **合计** | **61,232** | **57,143** | **4,089** | **17,150** |

演练事务处理 6,719 帖、重建 57,143 条正文关系，并把 2,271 帖的 `post_images_count` 修正为
权威数量。演练输出副本 SHA-256 为
`6435c12ec655320e7195c03b30d8a62bcad8321bfae0550621c999bea9087b2b`；`quick_check=ok`、外键
违规为 0。

保护性不变量在事务前后完全一致：

- `web_posts` 非图片字段：`51c265efa2cdbd8f4c69588a7ded1ed054be7258f2282a710b657673fb470458`
- `crawl_discovery_checkpoints`：`34a5b433d5281733ca6a07d4fe7f750653d946aeb0d0f318b46bc59470de0637`
- `crawl_discovery_seen_candidates`：`5ad57355938919cee6f037dc4cef9af13aa6342cc314abb2da5173593136629c`
- `xhs_discovery_checkpoints`：`dded8ebfd10868e4d1af2e8609f410bbbb764f8576a7a02e91aa7fd4e0ccdc82`
- `xhs_discovery_seen_candidates`：`9446934b2666432dcc2d9fc148201d924ad4b004aff4bccd6bac97737d475680`

## 5. 测试与静态验收

专属测试：

```bash
source .venv/bin/activate
python -m pytest \
  tests/test_historical_image_materialization.py \
  tests/test_local_image_backfill.py
```

结果为 5 passed。包含共享持久化回归的定向集合结果为 28 passed；根项目完整回归结果为
237 passed。改动文件 `py_compile`、Ruff、`git diff --check` 和冻结资产校验全部通过。

默认库 GC dry-run 扫描 `data/media` 文件 0、引用 0、无引用文件 0、删除 0；数据库前后 SHA 不变。
现有 17,150 张小红书历史图片仍位于原审计路径，只有到 H-03 才允许晋升到 `data/media`。

## 6. H-01 验收结论

- dry-run 没有改变默认数据库或已有图片文件。
- apply 在任何写入前都创建并验证 backup；副本关系精确等于 H-00 权威投影。
- 主表行数、所有非图片字段和发现 checkpoint/seen candidates 完全不变。
- 主程序与历史工具共享候选和持久化组件，没有新增平台专用投影或下载实现。
- H-01 通过，允许进入 H-02；默认库尚未关系重建，历史图片缺口仍为 39,993。
