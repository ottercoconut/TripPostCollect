# H-03 小红书历史正文图片补全验收

> 结论：H-03 通过，小红书历史正文图片已全部本地化；允许进入 H-04。整个历史数据阶段尚未完成，
> `HISTORICAL_DATA_COMPLETE=false`。

## 1. 范围与最终结果

本阶段只处理 H-00 冻结的 1,808 篇小红书图文记录及其正文图片。作者头像、作者主页图片、视频、
视频封面和关键词发现均不在本阶段写入范围内。

| 指标 | H-00 / 执行前 | H-03 完成后 | 验收 |
|---|---:|---:|---|
| 权威正文图片 | 17,416 | 17,416 | 不变 |
| `content` 关系 | 21,040 | 17,416 | 与权威投影相等 |
| 已验证本地关系 | 17,150 | 17,416 | 缺口归零 |
| 误分类/重复关系 | 3,624 | 0 | 归零 |
| `data/media/xhs` 文件 | 0 | 17,416 | 与关系相等 |
| 本地正文图片字节 | 0 | 4,674,991,926 | 全部可解码 |
| 头像本地关系 | 0 | 0 | 未下载 |
| 新增视频文件 | 0 | 0 | 未下载 |

最终数据库 SHA-256 为
`8f1c5c1dbb6f30622336cfaadc32c2ace6243eab8f6a671ff85bb0f3577e27d9`，本地图片清单摘要为
`4d5d79372f3b857a375ba805c861b72f363dc1d03a7f42978a48e8d62b64c7b4`。

## 2. 执行分段

### 2.1 既有库存晋升

- 登录预检使用显式账号 `xhs-a01`，账号保持 `active`，没有自动换号。
- 固定 10 帖首批通过后，以 25 帖上限连续执行；最终处理 1,791 篇、17,150 张、
  4,596,325,862 bytes。
- 每个旧文件先按真实字节识别扩展名，写入 staging，再调用主程序校验器与不可变路径晋升器。
- 晋升后逐文件同时复验旧源路径和长期路径，SHA、尺寸和 MIME 一致；没有删除旧文件。
- 状态文件：
  `data/runtime/image_materialization/historical-images-20260807-v1/xhs-existing-promotion-state.json`。

### 2.2 固定 10 帖在线缺口

- 只从数据库读取已存在帖子的签名 `note_url`，MediaCrawler 使用 `detail`，不执行 search 发现，
  `--no-import` 与 `--no-checkpoint-write` 为强制门禁。
- 关闭作者主页补采；MediaCrawler `_xhs_image_assets()` 只投影详情 `image_list` 正文图，视频仍由
  项目策略禁用。
- 首轮已完整下载并复验 10 篇、159 张，但旧行为门禁错误要求 detail 模式具有搜索分页事件，外层
  因此拒绝提交。修正后复用原 staging，不重复下载。
- 当前详情 CDN URL 会随请求时间重新生成。恢复时要求帖子集合、每帖图片数、连续索引、正文来源字段
  完全一致，且新旧 URL 均属于受信 `xhscdn.com`；数据库继续保留 H-00 权威 URL，并在
  `local_file` 同时记录实际下载 URL、下载资产键和
  `historical_xhs_detail_post_index_v1` 匹配模式。
- 最终提交 159 张、44,644,970 bytes，移除 20 条误分类行；报告：
  `data/runtime/image_materialization/historical-images-20260807-v1/h03/xhs-download-fixed10-resume2/report.json`。

### 2.3 剩余 7 帖在线缺口

- dry-run 精确得到 7 篇、107 张，数据库 SHA 前后不变。
- 正式批次下载、清单复验、长期晋升和同帖事务全部通过；提交 107 张、34,021,094 bytes，移除
  14 条误分类行。
- 报告：
  `data/runtime/image_materialization/historical-images-20260807-v1/h03/xhs-download-remaining7/report.json`。

## 3. 失败保护与恢复证据

本阶段出现的三类失败均在正文事务前停止：

1. 旧 `.jpg` 路径实际保存 WebP 字节，首次晋升在写文件和数据库前失败；改为按真实 MIME 重新
   staging 后通过。
2. 首个在线批次 159/159 张已完整暂存，但历史 detail 行为证据误用了搜索门禁；账号租约正常释放，
   正文关系未写入。
3. 第一次恢复发现旧权威 URL 与当前 CDN URL 不同，严格清单校验拒绝晋升；完成受信域、帖子、数量
   和索引证明后才启用小红书专用匹配，其他平台不允许使用该回退。

每个 apply 批次都在写入前生成 SQLite backup，并由 `sqlite_backup()` 独立执行 `quick_check` 与
外键检查。关键在线 backup：

- 固定批次恢复前：
  `data/backups/historical_images/historical-images-20260807-v1/h03/xhs-download-fixed10-resume2/trippostcollect.sqlite`，
  SHA-256 `a2b5ee838cefa2ce9a79d01e50207b0d94351e7305dca061212bbc90e01e2f74`；
- 剩余批次前：
  `data/backups/historical_images/historical-images-20260807-v1/h03/xhs-download-remaining7/trippostcollect.sqlite`，
  SHA-256 `2996aff17596755292c6ef307b395ecf7d4aa2f6ca87a2a5eb4a3920503386ee`。

## 4. 平台级验收

最终验收重新遍历 1,808 篇帖子并调用生产 `content_image_candidates()`：

- 权威正文图片、数据库 `content` 行、本地关系、被引用文件和目录实际文件均为 17,416；
- 每行的帖子、索引、H-00 权威 URL 与稳定资产键均与重新投影结果一致；
- 17,416 个文件全部重新解码，SHA、MIME、宽高和真实扩展名错误数为 0；
- 缺失文件 0、孤儿文件 0、头像本地关系 0、活动 XHS 租约 0；
- `PRAGMA quick_check=ok`，外键违规 0；
- `web_posts` 非图片字段与四张发现表的摘要仍与 H-00 完全一致。

测试与静态门禁：

```bash
source .venv/bin/activate
python -m py_compile \
  scripts/mediacrawler_crawl.py \
  scripts/xhs_runner.py \
  scripts/xhs_historical_images.py \
  src/trippostcollect/artifacts/historical_image_materialization.py \
  tools/MediaCrawler/media_platform/xhs/core.py
python -m ruff check \
  scripts/mediacrawler_crawl.py \
  scripts/xhs_runner.py \
  scripts/xhs_historical_images.py \
  src/trippostcollect/artifacts/historical_image_materialization.py
PYTHONPATH=. python -m pytest -q
python scripts/verify_frozen_files.py
```

根项目实现提交为 `f8defe5`、`a175289`、`71d7691`、`712eff1`、`70e4f27`、`230932d`；
MediaCrawler 内核并发修正提交为 `9c80a21`。所有提交信息均为中文。

## 5. 阶段结论

H-03 的验收标准全部满足，下一步只能进入 H-04 B站历史补全。H-04 必须继续遵守 H-02 冻结的
固定 10 帖首批、50 帖扩大批次、并发 1、单请求 30 秒和批次 3,600 秒门禁；不得重新执行关键词
发现，也不得在 B站平台验收通过前进入 H-05。
