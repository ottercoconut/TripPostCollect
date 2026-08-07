# 图片本地存储主程序 I-05 验收报告

前置提交：`e41799e`（I-04）

## 变更

- B站 article 详情链路在项目 `--download-images` 模式下下载正文图片；搜索预览
  `search_preview_urls` 不能产生下载任务。
- 图片请求复用当前 article Cookie、固定 User-Agent 和文章 Referer；每次手动重定向继续执行受限
  URL 校验，Cookie/请求头不会写入 manifest。
- 图片响应执行 20 MiB 预检和严格 raster MIME，再使用 I-03 的真实格式、Pillow、SHA 和原子
  staging 组件。
- 每张图最多 3 次；网络错误、超时、401/403、429 和 5xx 采用指数退避。最终失败写
  `fetch_status=failed`，不会伪造成功文件字段。
- B站 manifest 固定为
  `<batch>/bilibili/data/bili/image_manifest.jsonl`，通过原子 `.part` + `fsync` 更新。
- 图片可恢复失败使当前批次 `runtime_failed`、`resume_page` 保持当前页，失败帖子不进入 JSONL、
  `seen_ids`、已知候选或数据库。
- `summarize_output()` 已把 image manifest 与内容 JSONL 分开，避免 manifest 被误计为内容记录。
- `media_enabled` 仅反映项目图片模式，`video_enabled` 始终为 false；没有接入 MediaCrawler B站视频
  下载代码。

## 测试

```text
python -m pytest \
  tests/test_bilibili_article_detail.py \
  tests/test_bilibili_image_materialization.py \
  tests/test_bilibili_formal_route.py \
  tests/test_image_manifest.py \
  tests/test_image_materialization.py \
  tests/test_mediacrawler_import.py \
  tests/test_discovery_checkpoints.py \
  apps/admin_api/tests/test_readonly_api.py
66 passed

python -m ruff check \
  scripts/mediacrawler_crawl.py \
  src/trippostcollect/artifacts/image_proxy.py \
  src/trippostcollect/artifacts/image_manifest.py \
  tests/test_bilibili_image_materialization.py
All checks passed

python -m compileall -q src scripts/mediacrawler_crawl.py tests/test_bilibili_image_materialization.py
passed

python scripts/verify_frozen_files.py
Frozen file verification passed
```

fixture 覆盖 Opus 段落图、旧 article HTML 图和详情 fallback 图。新增调用级测试证明：

- manifest 的数量、顺序、稳定键和详情 `image_urls` 一致；
- 搜索预览、作者头像和视频文件新增为 0；
- 已知帖子 ID 在详情和图片请求前跳过；
- 503 连续失败只产生一行失败 manifest，当前页不推进且候选身份集合为空；
- manifest 不含测试 Cookie 或请求头字段。

## 不变量

- 所有下载测试使用 pytest `tmp_path`，没有写入项目 `data/media/`。
- 默认库 SHA-256 仍为
  `a7f78d3636025d61b442b50050afb2c36b7808772131d77df1c344bcdf371006`。
- MediaCrawler 工作区保持 clean；B站第三方视频代码未修改。

## 结论

`I_05_BILIBILI_IMAGES_READY=true`

B站平台侧 staging/manifest 已完成；统一根校验、长期晋升和导入编排仍在 I-10 接入。
