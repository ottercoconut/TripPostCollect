# 图片本地存储主程序 I-10 验收报告

前置根项目提交：`3200508`（I-09）

MediaCrawler 基线提交：
`1e88b234156dfe434041f459ff0a908184d942e8`

## 变更

- `scripts/mediacrawler_crawl.py --download-images` 已扩展为五平台统一正文图片模式；四个
  MediaCrawler 平台均向 child 传递 `--get_media true`，B站继续走根项目 article 下载器，视频
  入口始终关闭。
- 新增 `--media-root`。默认值为 `LOCAL_MEDIA_ROOT`；显式覆盖只接受项目 `temp/` 子目录，拒绝
  其他项目目录和项目外路径。
- 正式入库模式缺少 `--download-images` 会在平台执行前立即失败；遗留 `--get-media` 仍立即
  失败，并引导使用项目正文图片模式。
- 新增根项目统一物化编排：按每条有效记录关联 manifest，复验 schema、平台/帖子/资源键、
  来源顺序、数量、相对路径、SHA-256、真实 MIME、尺寸和字节数，再使用同一个内容哈希晋升
  函数生成 `MaterializedImage`。
- manifest 与 staging 必须位于当前项目受控目录。`staging_path` 按对应平台 data root 解析，
  不能跨 manifest 借用文件，也不能通过路径或符号链接逃逸。
- `collect_formal_records()` 新增本地图片完成谓词；未进入已完整验证身份集合的记录以
  `local_images_incomplete` 拒绝，不能计入新增目标或正式导入集合。
- 正式流程在字段/行为校验后复验图片并晋升，随后把完整 `MaterializedImage` 注入记录；
  `import_valid_records()` 以 `require_local_images=true` 执行整帖 SQLite 事务。
- `--no-import --download-images` 只复验 staging/manifest，不创建媒体根、不生成
  `MaterializedImage`、不打开 SQLite；该诊断结果不能代替正式导入完成。
- JSON 与 Markdown 摘要新增根级 `image_materialization` 证据，包括候选帖、期望/下载/复验/
  晋升/复用数、失败分类、manifest 路径及哈希和完成状态。

## 测试

```text
source .venv/bin/activate
python -m ruff check \
  scripts/mediacrawler_crawl.py \
  tests/test_mediacrawler_import.py \
  tests/test_mediacrawler_pagination.py
All checks passed

python -m pytest \
  tests/test_image_materialization.py \
  tests/test_image_persistence.py \
  tests/test_mediacrawler_import.py \
  tests/test_mediacrawler_pagination.py
50 passed
```

临时端到端 fixture 证明：

- B站、微博、XHS、抖音和知乎各一帖均通过同一个根项目验证/晋升函数，没有平台专用持久化
  分支；五张正文图均写入受控临时媒体根。
- 同一批五帖写入临时 SQLite 后得到 5 条 `web_posts`、5 条正文图片关系和 5 条非空
  `local_path`；第二次执行新增主表行为 0、更新 5，长期文件仍为 5。
- 第二次晋升 `promoted_images=0`、`reused_images=5`；`PRAGMA quick_check=ok` 且
  `foreign_key_check` 为空。
- manifest 缺失、staging 文件缺失、数量不符、身份不符和 SHA 不符五种 fixture 均
  `complete=false`，完整身份集合与 `MaterializedImage` 映射均为空，媒体根不存在。
- 诊断 fixture 在完整复验后 `validated_images=1`，但 `promoted_images=0`，媒体根和 SQLite
  文件均不存在。
- 正式记录选择在缺少本地图片完成身份时返回 `local_images_incomplete`，新增目标和
  `completion_met` 均不成立。

## 不变量

- 所有新增端到端测试只使用 pytest `tmp_path` 和临时 SQLite；没有访问真实平台或正式图片
  URL。
- 默认库 SHA-256 仍为
  `a7f78d3636025d61b442b50050afb2c36b7808772131d77df1c344bcdf371006`。
- 正式 `data/media/` 新增文件数为 0；MediaCrawler 嵌套仓库保持 clean，HEAD 仍为
  `1e88b234156dfe434041f459ff0a908184d942e8`。
- 父 runner 和冻结状态均未修改；I-11 之前不能把本步骤视为正式 runner 已完成。

## 结论

`I_10_ROOT_ORCHESTRATION_READY=true`

下一步 I-11 将把通用 runner、XHS runner、冻结状态和正式完成谓词接入本地图片完整性门禁。
