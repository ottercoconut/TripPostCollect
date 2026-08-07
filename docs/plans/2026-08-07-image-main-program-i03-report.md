# 图片本地存储主程序 I-03 验收报告

前置提交：`2cf61cd`（I-02）

## 变更

- 新增长期图片根 `data/media/` 和图片物化运行状态目录常量；代码导入不会自动创建目录。
- 新增归档图片验证组件：20 MiB 默认字节上限、100,000,000 默认像素上限、魔数、Pillow
  解码、真实 MIME/后缀、宽高和 SHA-256。
- 只允许 JPEG、PNG、WebP、GIF 和 AVIF；SVG、HTML、JSON、音视频、octet-stream、伪造后缀、
  截断文件和解码炸弹不会进入成功态。
- 新增流式 `.part` staging、文件和目录 `fsync`、`os.replace()` 原子发布，以及异常中断证据保留。
- 新增长期内容哈希路径、非安全帖子 ID 哈希编码、重复晋升复用和冲突拒绝。
- 管理端预览与归档共用 URL、响应 MIME、`Content-Length` 和有界读取校验；预览仍允许原有
  `image/*`，正式归档使用严格 raster allowlist。
- Pillow 已成为根项目显式运行依赖，并同步更新 `uv.lock`。

## 测试

```text
python -m pytest \
  tests/test_image_candidates.py \
  tests/test_image_manifest.py \
  tests/test_image_materialization.py \
  tests/test_mediacrawler_import.py \
  apps/admin_api/tests/test_readonly_api.py
52 passed

python -m ruff check \
  src/trippostcollect/artifacts/image_candidates.py \
  src/trippostcollect/artifacts/image_manifest.py \
  src/trippostcollect/artifacts/image_materialization.py \
  src/trippostcollect/artifacts/image_proxy.py \
  src/trippostcollect/core/paths.py \
  scripts/mediacrawler_crawl.py \
  tests/test_image_materialization.py
All checks passed

python -m compileall -q src scripts/mediacrawler_crawl.py tests/test_image_materialization.py
passed

python scripts/verify_frozen_files.py
Frozen file verification passed
```

专属测试还证明：绝对/穿越/符号链接路径被拒绝；相同 SHA 重复晋升只产生一个长期文件；目标内容
冲突不会被覆盖；模拟 `os.replace()` 中断只留下可识别 `.part`，不会出现成功文件。

## 不变量

- 全部文件测试使用 pytest `tmp_path`，没有写入项目 `data/media/`。
- 默认库 SHA-256 仍为
  `a7f78d3636025d61b442b50050afb2c36b7808772131d77df1c344bcdf371006`。
- MediaCrawler 工作区保持 clean；未修改默认库、配置、浏览器状态或既有图片。

## 结论

`I_03_IMAGE_MATERIALIZATION_READY=true`

本步骤完成纯文件安全与晋升能力；SQLite 整帖事务和本地元数据保护由 I-04 实现。
