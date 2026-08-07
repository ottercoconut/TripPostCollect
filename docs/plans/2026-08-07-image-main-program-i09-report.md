# 图片本地存储主程序 I-09 验收报告

前置提交：`d2db431`（I-08）

MediaCrawler 补丁提交：
`1e88b234156dfe434041f459ff0a908184d942e8`

## 变更

- `get_aweme_media()` 改为兼容性的严格图片入口，只调用 `get_aweme_images()`；图片列表为空立即
  返回，删除此前自动回退 `get_aweme_video()` 的行为。
- `_extract_note_image_assets()` 对每个 `images[]` 对象只选当前 `url_list` 的 fresh URL，并保留
  `uri`、连续来源序号和稳定资源键；不同签名查询参数不改变身份。
- 图片下载只复用当前存活的 `dy_client.get_aweme_media()`。本步骤不使用历史 URL、不刷新历史
  详情；历史刷新仍留在 H-07。
- Douyin 图片 store 复用共享 raster 校验和整帖原子 staging，写入
  `douyin/images/<aweme_id>/<index>.<真实扩展名>` 与 schema v1 manifest。
- JSONL 图片模式记录新增 `image_assets` 和 `image_list_source=aweme.images`，与
  `note_download_url` 使用同一投影；非图片模式和可选 ORM store 保持旧表字段契约。
- 搜索链路只为初筛有效的图文候选下载图片；图片成功后才写内容、累计候选和已处理集合。
  可恢复失败写失败 manifest，并保持当前 page、offset、search ID。
- `cover_url`、`video_download_url` 和 `music_download_url` 可继续作为文字记录的非正文元数据，
  但不进入 `image_assets`、下载请求、manifest 或图片目录。
- I-06 临时增加的 Weibo 图片身份 ORM 列改为非持久 transient 属性，避免已存在的可选
  MediaCrawler 数据库需要 schema 迁移，同时保留 JSONL 接口兼容。

## 测试

```text
cd tools/MediaCrawler
uv run pytest \
  tests/test_douyin_image_only.py \
  tests/test_douyin_store.py
7 passed

uv run pytest \
  tests/test_douyin_search_safety.py \
  tests/test_douyin_no_user_info.py \
  tests/test_douyin_image_only.py \
  tests/test_douyin_store.py \
  tests/test_weibo_no_user_info.py
36 passed

cd ../..
source .venv/bin/activate
python -m pytest \
  tests/test_image_candidates.py \
  tests/test_mediacrawler_pagination.py \
  tests/test_discovery_checkpoints.py
36 passed

python -m ruff check --ignore F403,F405,F541 \
  tools/MediaCrawler/tools/image_manifest.py \
  tools/MediaCrawler/store/douyin/douyin_store_media.py \
  tools/MediaCrawler/store/douyin/__init__.py \
  tools/MediaCrawler/media_platform/douyin/core.py \
  tools/MediaCrawler/tests/test_douyin_image_only.py \
  tools/MediaCrawler/tests/test_douyin_store.py
All checks passed
```

fixture 与 spy 证明：

- 图文作品只请求一条 note 图片 URL；保存真实 PNG 后缀和 MIME，并写 `douyin:uri:*` manifest；
- 空图片对象和视频候选的图片请求、`get_aweme_video()`、视频 store 调用全部为 0；
- 封面、视频和音乐 URL 不出现在候选或 manifest；
- 同一 URI 的 host/path/signature 变化不改变资源键；无 URI fallback 也忽略签名查询参数；
- 图片返回空响应时只有失败 manifest，没有成功文件；
- 搜索图片失败时内容 store 未调用、`candidate_count=0`，page=1、offset=0、search ID 为空，
  与失败前前沿完全一致；
- 现有抖音搜索响应安全、游标恢复、匿名化存储和微博 ORM 兼容回归保持通过。

## 不变量

- 测试只使用 fake client、内存 SQLite 和 pytest `tmp_path`，没有视频、音乐或正式图片字节访问。
- 默认库 SHA-256 仍为
  `a7f78d3636025d61b442b50050afb2c36b7808772131d77df1c344bcdf371006`。
- MediaCrawler 嵌套仓库在补丁提交后 clean；正式视频 store 文件新增为 0。

## 结论

`I_09_DOUYIN_IMAGES_READY=true`

五个平台的 staging/manifest 入口均已完成。下一步 I-10 开始接入根执行器统一复验、长期晋升、
事务导入和完成谓词。
