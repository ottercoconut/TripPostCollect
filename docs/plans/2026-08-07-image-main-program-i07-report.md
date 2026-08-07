# 图片本地存储主程序 I-07 验收报告

前置提交：`4d7f36a`（I-06）

MediaCrawler 补丁提交：
`0e7467ecdd23cee0d9535b70a2d45213eda666d1`

## 变更

- 保留 `get_notice_media()` 的图片专用语义：启用媒体时只调用 `get_note_images()`；
  `get_notice_video()` 和视频 store 不进入调用链。
- 每个 `note_detail.image_list` 对象只按 `url_default`、`url`、`url_pre` 优先级选择一个权威
  URL，不再把同一对象的多个传输变体分别下载。
- 使用与根项目一致的 XHS 稳定路径身份；不同 CDN 主机、协议或查询参数指向同一
  `/notes_pre_post/`、`/notes_post/`、`/notes/` 资产时只保留一个候选。
- 新抓取记录保存连续的 `image_assets` 和 `image_list_source=note_detail.image_list`，内容
  `image_list` 与实际下载候选使用同一投影。
- XHS 图片复用 I-06 引入的整帖原子 staging：全部图片先完成大小、像素、真实 raster 格式和
  Pillow 校验，再一次性晋升为 `xhs/images/<note_id>/<index>.<真实扩展名>`。
- schema v1 manifest 写入 `<save_data_path>/xhs/image_manifest.jsonl`；失败图片只写失败行，
  不产生伪成功路径或半帖目录。
- 正式搜索链路只为初筛有效的非视频图文记录下载图片。图片完成后才写内容记录并累计候选；失败时
  记录 `image_download_failed`，页码和 search ID 保持当前恢复位置。
- 本步骤没有执行根项目正式 runner、历史 backfill 或长期媒体晋升。

## 测试

```text
cd tools/MediaCrawler
uv run pytest \
  tests/test_xhs_image_download.py \
  tests/test_xhs_media_policy.py
6 passed

uv run pytest tests/test_xhs_creator_enrichment.py
10 passed

uv run pytest tests/test_xhs_discovery_memory.py
6 passed

uv run pytest tests/test_xhs_popup_guard.py
5 passed

cd ../..
source .venv/bin/activate
python -m pytest \
  tests/test_xhs_pool.py \
  tests/test_xhs_discovery.py \
  tests/test_image_candidates.py
46 passed

python -m ruff check --ignore F403,F405,F541 \
  tools/MediaCrawler/tools/image_manifest.py \
  tools/MediaCrawler/store/xhs/xhs_store_media.py \
  tools/MediaCrawler/store/xhs/__init__.py \
  tools/MediaCrawler/media_platform/xhs/core.py \
  tools/MediaCrawler/tests/test_xhs_image_download.py \
  tools/MediaCrawler/tests/test_xhs_media_policy.py
All checks passed
```

fixture 和 spy 验证证明：

- 一个含 default/url/pre 三种 URL 的图片对象只发起一次请求，只写一个文件和一行 manifest；
- `.jpg?format=...` 来源返回 PNG 时，实际文件和 MIME 分别为 `.png`、`image/png`；
- 同一稳定路径的 CDN/协议/查询变体不会重复下载；
- 下载失败只有失败 manifest，没有成功文件或成功元数据；
- 作者头像不进入图片资产投影；
- 图片模式与禁用媒体模式下，视频方法和视频 store 调用次数都为 0；
- 现有 XHS 发现记忆、创作者补全和弹窗保护回归均通过。

## 不变量

- 新增图片测试仅使用 pytest `tmp_path`，没有写正式批次或长期媒体目录。
- 默认库 SHA-256 仍为
  `a7f78d3636025d61b442b50050afb2c36b7808772131d77df1c344bcdf371006`。
- 默认库带本地路径的 XHS 正文图片仍为 17,150 行；逐文件复验结果为缺失 0、数据库 SHA 与实际
  文件 SHA 不一致 0。路径关系随未变化的默认库保持不变。
- I-00 的既有本地图片排序清单摘要仍为
  `a6031e2401bdfe224fe98835ff4726a1bac6f0cbfdbe85cb8ef4d3cb06c44ad5`。
- MediaCrawler 嵌套仓库在补丁提交后 clean；没有创建或修改任何历史 XHS 图片。

## 结论

`I_07_XHS_IMAGES_READY=true`

XHS 新抓取 staging/manifest 已完成。当前 17,150 个历史文件仍冻结，必须等
`MAIN_PROGRAM_READY=true` 后才进入 H 阶段补全与迁移。
