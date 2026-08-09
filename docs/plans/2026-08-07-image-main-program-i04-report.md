# 图片本地存储主程序 I-04 验收报告

前置提交：`5fe46a6`（I-03）

## 变更

- `row_for_record()` 可接收完整 `MaterializedImage` 集合，并按平台、帖子、角色、序号、来源字段、
  稳定键和 URL 全量匹配后注入本地元数据。
- `raw_image_json` 现在保存来源序号、稳定资源键和 `local_file` 证据；路径、MIME、尺寸和 SHA 仍以
  SQLite 专用列为查询权威。
- `upsert_web_post()` 在任何写操作前复验新文件，并按“稳定键、规范化 URL、受限旧序号”顺序匹配
  已有关系；稳定键冲突不会退化为 URL 误复用。
- 新 staging 和长期文件严格要求真实后缀；已有本地关系只有在实际 MIME、尺寸和 SHA 复验通过时
  才允许由 upsert 保留。
- content `image_index` 按角色从 0 连续；头像继续为独立 URL-only 关系，不计入
  `post_images_count` 或正文本地完整性。
- 每帖写入使用 SQLite savepoint。文件缺失、元数据不符、数量/身份不符或任一图片 INSERT 失败时，
  主表和该帖全部图片关系恢复到事务前状态。

## 测试

```text
python -m pytest \
  tests/test_image_candidates.py \
  tests/test_image_manifest.py \
  tests/test_image_materialization.py \
  tests/test_image_persistence.py \
  tests/test_mediacrawler_import.py \
  tests/test_mediacrawler_pagination.py \
  apps/admin_api/tests/test_readonly_api.py
71 passed

python -m ruff check \
  src/trippostcollect/artifacts/image_candidates.py \
  src/trippostcollect/artifacts/image_manifest.py \
  src/trippostcollect/artifacts/image_materialization.py \
  src/trippostcollect/artifacts/image_proxy.py \
  scripts/mediacrawler_crawl.py \
  tests/test_image_persistence.py \
  tests/test_mediacrawler_import.py
All checks passed

python -m compileall -q src scripts/mediacrawler_crawl.py tests/test_image_persistence.py
passed

python scripts/verify_frozen_files.py
Frozen file verification passed
```

临时 SQLite 验证覆盖：新帖整帖提交、重复执行幂等、签名 URL 更新、稳定键变化、文件丢失、图片
重排、删除、新增、错误物化身份和 SQL 第二张图片插入失败。严格模式均满足：

```text
post_images_count = content rows = content rows with local_path
```

并且 `PRAGMA foreign_key_check` 为 0。

## 不变量

- 数据库测试只使用内存 SQLite；媒体测试只使用 pytest `tmp_path`。
- 默认库 SHA-256 仍为
  `a7f78d3636025d61b442b50050afb2c36b7808772131d77df1c344bcdf371006`。
- MediaCrawler 工作区保持 clean；未修改默认关系或晋升既有 XHS 文件。

## 结论

`I_04_IMAGE_PERSISTENCE_READY=true`

五平台的实际下载入口尚未接入；从 I-05 开始逐平台生成 staging 和 manifest。
