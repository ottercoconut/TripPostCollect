# 图片本地存储主程序 I-02 验收报告

前置提交：`ffce6d3`（I-01）

## 变更

- 正文候选已使用最终平台稳定资源键：B站 `bfs` 资产、小红书稳定资源路径、微博 `pid`、抖音
  `uri`、知乎去尺寸/格式变换路径；无法获得平台 ID 时使用确定性的 SHA-256 fallback。
- 微博和抖音投影可按来源序号读取非敏感 `image_assets` 元数据，当前旧记录仍可使用 URL fallback。
- 新增严格 `image_manifest` schema v1、成功/失败状态约束、确定性 JSONL 序列化与可复现 SHA-256。
- 新增逐帖候选/manifest 数量、平台、帖子、角色、序号、来源字段、稳定键和 URL 身份校验。
- manifest 拒绝绝对路径、路径穿越、Windows 路径、重复身份、未知字段、缺字段、未知 schema、
  非法成功元数据和 Cookie/请求头等 schema 外字段。

## 测试

```text
python -m pytest \
  tests/test_image_candidates.py \
  tests/test_image_manifest.py \
  tests/test_mediacrawler_import.py \
  tests/test_mediacrawler_pagination.py
40 passed

python -m ruff check \
  src/trippostcollect/artifacts/image_candidates.py \
  src/trippostcollect/artifacts/image_manifest.py \
  scripts/mediacrawler_crawl.py \
  tests/test_image_manifest.py
All checks passed

python scripts/verify_frozen_files.py
Frozen file verification passed
```

稳定键测试覆盖协议、CDN 域名、B站变换后缀、知乎尺寸/格式后缀和签名查询参数变化，并验证不同
资产不会使用相同键。manifest round-trip 排序和 SHA 在输入顺序变化后保持一致。

## 不变量

- 默认库只读投影仍为：B站 18,050、微博 6,384、抖音 4,528、知乎 10,765、小红书 17,416。
- 默认库 SHA-256 仍为
  `a7f78d3636025d61b442b50050afb2c36b7808772131d77df1c344bcdf371006`。
- MediaCrawler 工作区保持 clean；未产生下载、staging 或长期图片文件。

## 结论

`I_02_IMAGE_MANIFEST_READY=true`

本步骤只建立身份与数据契约。实际字节验证、原子 staging 和长期晋升由 I-03 实现。
