# 图片本地存储主程序 I-08 验收报告

前置提交：`29b6215`（I-07）

MediaCrawler 补丁提交：
`b851ffd42fad8ab09a4249d5053e92d382d622fb`

## 变更

- Zhihu client 新增 `get_content_image()`：复用当前代理、Cookie、User-Agent，以内容页作为
  Referer，并使用流式读取把单图响应限制在 20 MiB 内。
- 新增 `store/zhihu/zhihu_store_media.py`，复用共享 raster 校验和整帖原子 staging；真实格式
  文件写入 `zhihu/images/<content_id>/<index>.<真实扩展名>`。
- schema v1 manifest 写入 `<save_data_path>/zhihu/image_manifest.jsonl`；稳定资源键与根项目一致，
  会去除 zhimg 尺寸/格式变换后再计算身份。
- `zhihu_content_image_assets()` 只接受 answer/article、`detail_observed` 且来自正文 HTML 的
  `image_list`。它显式排除 `/equation`、`avatar_url`、`author_profile_url` 和所有 zvideo。
- 搜索响应已经携带正文图时标记为 `detail_observed` 并直接下载；搜索缺图时必须成功取得并合并
  answer/article 详情，随后走同一图片入口。
- `request_failed`、`parse_failed`、`search_payload`、无图和 zvideo 均无法进入图片下载成功态。
- 搜索链路先完成图片 staging/manifest，再写内容 JSONL并累计候选。图片失败时当前页不推进，
  内容不跨过安全前沿。
- 指定详情模式同样只在详情已观察且存在权威正文图时下载；失败详情仍可保留诊断记录，但不会产生
  成功图片 manifest。
- JSON/JSONL 输出新增 `image_list_source` 和逐图 `image_assets`；可选 ORM store 在写旧表前移除
  这两个非表字段，避免接口升级导致构造错误。

## 测试

```text
cd tools/MediaCrawler
uv run pytest \
  tests/test_zhihu_image_download.py \
  tests/test_zhihu_detail_images.py
8 passed

uv run pytest \
  tests/test_zhihu_image_download.py \
  tests/test_zhihu_detail_images.py \
  tests/test_zhihu_search_detail.py \
  tests/test_store_factory.py
20 passed

cd ../..
source .venv/bin/activate
python -m pytest \
  tests/test_image_candidates.py \
  tests/test_mediacrawler_pagination.py
24 passed

python -m ruff check --ignore F403,F405,F541 \
  tools/MediaCrawler/tools/image_manifest.py \
  tools/MediaCrawler/store/zhihu/zhihu_store_media.py \
  tools/MediaCrawler/store/zhihu/__init__.py \
  tools/MediaCrawler/model/m_zhihu.py \
  tools/MediaCrawler/media_platform/zhihu/client.py \
  tools/MediaCrawler/media_platform/zhihu/core.py \
  tools/MediaCrawler/tests/test_zhihu_image_download.py \
  tools/MediaCrawler/tests/test_zhihu_detail_images.py
All checks passed
```

fixture、manifest 和 spy 证明：

- 搜索正文图不额外请求详情，详情补全正文图正确合并，两者使用同一下载/manifest 入口；
- 当前代理、Cookie、User-Agent 和具体内容 Referer 被图片 client 复用，但敏感请求字段不写 manifest；
- `.jpg` 来源返回 PNG 时保存为 `.png`，manifest MIME 为 `image/png`；
- `/equation?`、头像、作者主页、zvideo cover 的图片请求次数全部为 0；
- 请求失败和解析失败不能产生图片请求、文件或成功 manifest；
- 图片下载失败只写失败 manifest，没有路径或其他成功元数据；
- 现有搜索详情合并、指定详情信号和 store factory 回归保持通过。

## 不变量

- 图片与 client 测试只使用 fake response 和 pytest `tmp_path`，没有访问正式内容或写正式媒体目录。
- 默认库 SHA-256 仍为
  `a7f78d3636025d61b442b50050afb2c36b7808772131d77df1c344bcdf371006`。
- MediaCrawler 嵌套仓库在补丁提交后 clean；没有视频文件、zvideo 图片任务或作者资源文件。

## 结论

`I_08_ZHIHU_IMAGES_READY=true`

知乎新抓取图片适配完成。抖音严格 images-only 是最后一个平台侧步骤，随后进入根执行器统一编排。
