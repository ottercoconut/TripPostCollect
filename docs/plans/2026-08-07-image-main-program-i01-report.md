# 图片本地存储主程序 I-01 验收报告

前置提交：`3ba6646`（I-00）

## 变更

- 新增 `ImageCandidate` 和五平台 `content_image_candidates()` 显式白名单投影。
- B站只接受详情已观察的 `image_urls`；微博只接受归一 `image_list`；抖音只接受
  `note_download_url`；知乎排除公式；小红书每个对象只选一个 URL 并折叠同图 CDN 变体。
- 头像由独立 `author_avatar_reference()` 保留为 URL-only 关系，不再进入正文候选。
- `row_for_record()` 和 `validate_formal_record()` 已切换到显式投影；递归
  `dedupe_image_urls()` 及其辅助入口已删除。
- 本步骤只提供确定性的 URL 摘要资源键；第 I-02 步将按平台资产 ID 完成最终稳定资源键和 manifest
  schema。

## 专属测试与回归

```text
python -m pytest tests/test_image_candidates.py
13 passed

python -m ruff check \
  src/trippostcollect/artifacts/image_candidates.py \
  scripts/mediacrawler_crawl.py \
  tests/test_image_candidates.py
All checks passed

python -m pytest \
  tests/test_mediacrawler_import.py \
  tests/test_bilibili_article_detail.py \
  tests/test_mediacrawler_pagination.py \
  tests/test_discovery_checkpoints.py
33 passed
```

## 默认库只读投影

| 平台 | 帖子 | 权威正文图 |
|---|---:|---:|
| bilibili | 3,006 | 18,050 |
| weibo | 1,007 | 6,384 |
| douyin | 461 | 4,528 |
| zhihu | 437 | 10,765 |
| xhs | 1,808 | 17,416 |

结果与工程文档 I-01 固定验收数量完全一致。读取通过只读 SQLite 连接完成，没有重建关系或写库。

## 结论

`I_01_IMAGE_CANDIDATES_READY=true`

尚未开始下载、manifest、长期目录或数据库关系改造；这些工作受后续 I 阶段和
`MAIN_PROGRAM_READY` 门禁约束。
