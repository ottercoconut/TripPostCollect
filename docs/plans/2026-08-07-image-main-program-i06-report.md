# 图片本地存储主程序 I-06 验收报告

> 历史快照：本文记录 I-06 当时行为；当前候选失败处理以正式契约的 `candidate_skipped` 规则为准。

前置提交：`4ad1c91`（I-05）

MediaCrawler 补丁提交：
`ab6748edd7f4f321fc4dd23abe7186c12b99d4e3`

## 变更

- 微博正文图片权威来源固定为 `mblog.pics`。记录同时保存 `image_list_source=mblog.pics`
  和逐图 `image_assets`，后者明确携带 `pid`、URL 与连续来源序号。
- `get_note_images()` 复用现有 `wb_client.get_note_image()`，但不再从 URL 猜扩展名，也不再按
  `pid` 平铺文件；下载参数完整传递微博 note ID、pid、来源序号和 URL。
- 新增 MediaCrawler 共用图片 staging 组件：限制 20 MiB/100 MP，只接受
  JPEG、PNG、WebP、GIF、AVIF，经 Pillow 解码校验后使用实际格式扩展名。
- 一个帖子的全部图片先完成内存校验，再写入同一临时目录并 `fsync`；仅当整帖成功时才原子晋升为
  `weibo/images/<note_id>/<index>.<真实扩展名>`。部分失败不会留下已晋升的半帖目录。
- schema v1 manifest 固定写到 `<save_data_path>/weibo/image_manifest.jsonl`，其
  `staging_path` 相对 `<save_data_path>`，因此根项目可在平台 data root 下安全解析。
- manifest 以 `(platform, post, role, source_index)` 幂等更新，采用 `.part`、`fsync` 和
  `os.replace`。失败项只写 `fetch_status=failed`，不带路径、大小、MIME、尺寸或 SHA 成功元数据。
- 图片处理只在微博记录通过正文、时间、作者、粉丝可观测、正文图片和互动字段初筛后发生。
  已知 ID 仍在详情/图片处理前过滤；图片失败时帖子不写 JSONL、不进入候选累计，恢复页保持当前页。
- 图片枚举完全不读取 `user.avatar*`、作者主页或其他用户媒体字段；无图微博不会进入正文图片下载。
- MediaCrawler `WeiboNote` 模型补充两个图片来源元数据列，保持 JSON/JSONL 和可选 ORM 存储接口一致。

## 测试

```text
cd tools/MediaCrawler
uv run pytest \
  tests/test_weibo_image_download.py \
  tests/test_weibo_store.py
7 passed

uv run pytest \
  tests/test_weibo_image_download.py \
  tests/test_weibo_store.py \
  tests/test_weibo_no_user_info.py \
  tests/test_store_factory.py \
  tests/test_weibo_empty_search.py
20 passed

cd ../..
source .venv/bin/activate
python -m pytest \
  tests/test_image_manifest.py \
  tests/test_mediacrawler_import.py
17 passed

python -m ruff check --ignore F403,F405,F541 \
  tools/MediaCrawler/tools/image_manifest.py \
  tools/MediaCrawler/store/weibo/weibo_store_media.py \
  tools/MediaCrawler/store/weibo/__init__.py \
  tools/MediaCrawler/media_platform/weibo/core.py \
  tools/MediaCrawler/tests/test_weibo_image_download.py \
  tools/MediaCrawler/tests/test_weibo_store.py
All checks passed
```

fixture 和调用级测试证明：

- 两张 `mblog.pics` 图片分别保留 note ID、pid、0/1 序号和原 URL；
- 带 `.jpg?query` 的 URL 返回 PNG 时实际文件为 `.png`、manifest MIME 为 `image/png`；
- 第二张非 raster 响应时整帖目录不晋升，也不产生成功 manifest；
- 下载返回空响应时仅产生失败 manifest；
- 作者头像、主页和无图内容不能进入图片资产列表；
- 默认库中的已知微博 ID 在媒体方法前跳过，无效微博也不触发图片方法；
- 图片失败的恢复事件保持当前页，`candidate_count=0`，帖子内容未写入；
- 根项目能够用同一 schema 解析 MediaCrawler 样例，并与 `weibo:pid:*` 候选精确对应。

## 不变量

- 所有图片测试只写 pytest `tmp_path`；没有写入项目 `data/media/` 或正式批次目录。
- 默认库 SHA-256 仍为
  `a7f78d3636025d61b442b50050afb2c36b7808772131d77df1c344bcdf371006`。
- MediaCrawler 嵌套仓库在补丁提交后 clean；没有触发微博视频下载代码。

## 结论

`I_06_WEIBO_IMAGES_READY=true`

微博平台侧 staging/manifest 已完成；正式 runner 的五平台统一启用、严格校验、长期晋升与导入仍在
I-10 接入。
