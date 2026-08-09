# 图片本地存储主程序 I-11 验收报告

前置根项目提交：`dce0ad9`（I-10）

MediaCrawler 基线提交：
`1e88b234156dfe434041f459ff0a908184d942e8`

## 变更

- 通用 `crawl_runner.py` 对 B站、微博、抖音和知乎正式 child 无条件冻结并传递
  `--download-images --media-root <LOCAL_MEDIA_ROOT>`；配置中的 `download_images` 被视为已删除
  选项，`--get-media` 不可达。
- `xhs_runner.py` 不再读取目标级图片开关，正式 child 固定传递同一项目图片参数；冻结计划、
  阻塞计划和预执行失败计划均明确记录 `local_image_storage_required=true` 与正式媒体根。
- `config/xhs_targets.json` 和三个仍保留的 XHS one-off schema v2 配置已删除
  `download_images`。`load_target()` 拒绝遗留字段，并在验证后注入不可配置的
  `local_image_storage_required=true` 契约。
- 非正式容量评估入口 `info_collection_benchmark.py` 同步固定传递项目图片模式，避免 I-10
  正式入库参数收紧后出现调用错误。
- 新增 runner 侧独立 `image_completion.py`：重新计算每个 manifest 和聚合 SHA-256，校验
  staging/晋升模式、期望/下载/复验/晋升计数、失败集合和清单路径，不盲信 child 的布尔值。
- `artifacts_verified` 现在要求 child summary 存在且本地图片 manifest 证据完整；诊断模式要求
  staging 完整且 `promotion_required=false`，正式模式要求晋升完整。
- `persistence_verified` 现在逐条读取正式身份对应的 `web_posts` 与正文图片关系，检查图片计数、
  连续序号、非空本地路径、长期根边界、文件字节 SHA、MIME、尺寸、`quick_check` 和外键。
- 通用 runner 和 XHS runner 只有原有完成谓词与图片持久化谓词同时通过才解冻
  `task_finalized`。manifest 失败使 artifact 阶段失败，数据库/文件失败使 persistence 阶段失败。
- 通用 campaign 清理及 XHS `commit_child_discovery()` 都增加图片持久化完成前提；图片失败时保留
  当前 summary、候选计数和安全发现前沿，不把任务误标为已完成。

## 测试

```text
source .venv/bin/activate
python -m ruff check \
  src/trippostcollect/artifacts/image_completion.py \
  src/trippostcollect/xhs/config.py \
  src/trippostcollect/xhs/discovery.py \
  scripts/crawl_runner.py \
  scripts/xhs_runner.py \
  scripts/info_collection_benchmark.py \
  tests/test_bilibili_formal_route.py \
  tests/test_xhs_pool.py \
  tests/test_discovery_checkpoints.py \
  tests/test_xhs_discovery.py \
  tests/test_image_runner_contract.py
All checks passed

python -m pytest \
  tests/test_bilibili_formal_route.py \
  tests/test_xhs_pool.py \
  tests/test_discovery_checkpoints.py \
  tests/test_xhs_discovery.py \
  tests/test_image_runner_contract.py
52 passed

python -m pytest tests/test_frozen_files.py
1 passed
```

验收证据：

- 通用 runner 在临时数据库上真实生成 4 个 dry-run 冻结状态；四个平台计划均包含固定图片要求、
  正式媒体根和 `--download-images`，均不含 `--get-media`，且 command 阶段保持 frozen。
- B站正式路由和 XHS child 命令测试均验证媒体根与项目常量完全相同。
- 完整临时 manifest 重新哈希通过；只追加一个换行后 runner 侧 SHA 校验立即失败。
- 临时 SQLite/媒体根的一帖一图关系通过独立一致性校验；删除长期文件后 persistence 立即失败。
- artifact 阶段失败 fixture 证明 `persistence_verified` 与 `task_finalized` 都保持 frozen。
- XHS child 即使声明 `import_completion_met=true`，只要 runner 图片复核为 false，campaign summary
  与候选计数仍保留，`imported_target=false`。
- 目标配置带旧 `download_images=false` 也会被拒绝，不能恢复 URL-only 兼容分支。

## 不变量

- 所有 dry-run、SQLite、manifest 和长期文件验证均位于 pytest `tmp_path`，没有启动真实平台
  child，没有下载真实字节。
- 默认库 SHA-256 仍为
  `a7f78d3636025d61b442b50050afb2c36b7808772131d77df1c344bcdf371006`。
- 正式 `data/media/` 新增文件数为 0；MediaCrawler 嵌套仓库保持 clean，HEAD 仍为
  `1e88b234156dfe434041f459ff0a908184d942e8`。
- 用户冻结文件校验通过；默认内容库和正式媒体根没有变化。

## 结论

`I_11_RUNNER_IMAGE_GATES_READY=true`

下一步 I-12 执行五平台主程序综合回归、全量静态检查和真实 `--no-import` 小样；任何真实小样
仍只能生成 staging/manifest，不能写默认库或正式媒体根。
