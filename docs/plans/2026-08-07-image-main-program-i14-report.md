# 五平台正文图片本地存储主程序 I-14 总验收报告

> 执行日期：2026-08-07（Asia/Shanghai）  
> 实现版本：`image-main-v1`  
> 根项目验收前置提交：`d11c4ab83dbd0370903c99a6d38fcec42a8f9a41`  
> MediaCrawler 提交：`6e7475d4a3501a346ec0eccb592f5598b9b9718b`

## 1. 最终结论

```text
MAIN_PROGRAM_READY=true
HISTORICAL_DATA_COMPLETE=false
```

I-00 至 I-13 均按独立步骤完成并提交；I-14 再次运行根项目、MediaCrawler、平台图片专项、五平台
真实 `--no-import` 样本字节复验、冻结治理和默认库全量不变量检查。五个平台以后新抓图文已经具备
显式正文图投影、当前会话下载、原子 manifest、根项目安全复验、长期晋升和 SQLite 整帖事务。
正式 runner 无法 URL-only 完成；头像等非正文资源在下载前自动过滤，视频/音乐路径保持禁用。

本结论只代表主程序可正确处理以后新抓记录。当前数据库历史图片尚未按 H 阶段补全，不能汇报
`HISTORICAL_DATA_COMPLETE=true`，本轮也没有启动任何 H-00 至 H-08 操作。

## 2. 可追溯提交序列

根项目基线为 `b28195f`，主程序提交按实施顺序为：

| 步骤 | 提交 | 内容 |
|---|---|---|
| I-00 | `3ba6646` | 冻结数据库、现有图片、MediaCrawler 和文档基线 |
| I-01 | `ffce6d3` | 五平台权威正文图显式投影和噪声过滤 |
| I-02 | `2cf61cd` | 稳定资产身份与 schema v1 manifest |
| I-03 | `5fe46a6` | staging 文件安全复验、内容寻址和原子晋升 |
| I-04 | `e41799e` | 本地图片关系、整帖 savepoint、既有映射保留 |
| I-05 | `4ad1c91` | B站 article 详情正文图下载与 manifest |
| I-06 | `4d7f36a` | 微博根项目 manifest 验证与报告 |
| I-07 | `29b6215` | XHS 根项目 manifest 验证与报告 |
| I-08 | `d2db431` | 知乎根项目 manifest 验证与报告 |
| I-09 | `3200508` | 抖音严格 images-only 验证与报告 |
| I-10 | `dce0ad9` | 五平台根物化、晋升和导入编排 |
| I-11 | `df82d0e` | 通用/XHS runner、本地图片冻结阶段与完成谓词 |
| I-12 修复 | `e233082` | 两类平台 staging 布局兼容 |
| I-12 清理 | `f59ad75` | 全量 Ruff 既有未使用导入清理 |
| I-12 报告 | `e3c755d` | 综合回归和五平台真实小样证据 |
| I-13 | `d11c4ab` | 治理/平台/运维文档、冻结哈希和不可变标志 |
| I-14 | 本报告所在 release commit | `image-main-v1` 总验收 |

MediaCrawler 基线为 `78a2b8a`，平台补丁为：

| 平台/修复 | 提交 |
|---|---|
| 微博 staging + manifest | `ab6748e` |
| XHS staging + manifest | `0e7467e` |
| 知乎 staging + manifest | `b851ffd` |
| 抖音严格 images-only + manifest | `1e88b23` |
| 抖音 chunk-framed 多 JSON 文档流解析 | `6e7475d` |

## 3. 五平台实现闭环

| 平台 | 显式权威投影 | 当前会话下载与稳定键 | 自动排除 | staging / manifest | 长期与数据库 |
|---|---|---|---|---|---|
| B站 | 详情观察后的 `image_urls` | article Cookie/Referer；BFS 逻辑路径 | 搜索预览、封面、头像、视频 | 根 article 分支原子写入 | `data/media/bilibili` + 同帖事务 |
| 微博 | `mblog.pics` → `image_list` | 移动搜索会话；优先 `pid` | 用户头像、封面、视频缩略图 | `data/weibo/image_manifest.jsonl` | `data/media/weibo` + 同帖事务 |
| 小红书 | 详情 `image_list` 每对象择一 URL | 隔离账号会话；notes 稳定路径 | 头像、作者主页、封面、视频 | `data/xhs/image_manifest.jsonl` | `data/media/xhs` + 同帖事务 |
| 抖音 | `note_download_url` | 当前 `dy_client`；优先 `images[].uri` | 封面、视频、音乐、头像 | `data/douyin/image_manifest.jsonl` | `data/media/douyin` + 同帖事务 |
| 知乎 | 正文/详情 `image_list` | 当前知乎会话；zhimg 逻辑路径 | 公式、头像、作者主页、封面、zvideo | `data/zhihu/image_manifest.jsonl` | `data/media/zhihu` + 同帖事务 |

作者头像可以保留 `image_role=author_avatar` 的 URL-only 参考关系，但不会生成正文候选、下载、
manifest、本地文件或 `post_images_count`；因此“自动忽略头像下载”与现有作者展示字段并不冲突。

五平台正式 child 全部由 runner 强制带 `--download-images --media-root <data/media>`。旧
`--get-media` 直接失败；XHS 配置拒绝旧 `download_images` 字段；`--no-import` 只能在
`promotion_required=false` 下验证 staging，不能替代正式持久化。`artifacts_verified` 重验 manifest
和计数，`persistence_verified` 重验 SQLite、本地文件、SHA/MIME/尺寸、连续 index、quick check 和
外键。任一图片不完整时后续阶段保持 frozen，安全 checkpoint 不前移。

## 4. I-14 回归结果

### 4.1 根项目

```text
python -m pytest -q
234 passed in 1.88s

python -m ruff check src scripts tests apps/admin_api
All checks passed!

python -m compileall -q src scripts tests
passed

python scripts/verify_frozen_files.py
Frozen file verification passed.
```

主链专项再次执行 10 个测试文件，共 `113 passed`；覆盖五平台投影、manifest、安全复验、晋升、
SQLite 事务、通用/XHS runner 阶段、checkpoint 失败安全和临时 SQLite/媒体根端到端。

### 4.2 MediaCrawler

完整 `uv run pytest -q` 结果：

- `162 passed`；
- `8 skipped`：全部为本机未启动 MongoDB 的真实连接测试；
- `6 failed`：仅 `test_proxy_ip_pool.py` 3 项和 `test_redis_cache.py` 3 项，均为
  `127.0.0.1:6379 Connection refused`；
- 图片、平台、登录、行为、分页和抖音流解析测试 0 失败。

排除上述外部服务文件后的固定集合为 `160 passed`。八个平台图片专项文件再次得到
`28 passed`；额外的抖音搜索安全 + image-only + store 组合为 `32 passed`。唯一非外部依赖 warning
仍是 SQLAlchemy 2.0 `declarative_base()` 弃用提示，不影响本功能。

### 4.3 五平台真实样本重新复验

I-12 的最终真实样本仍是当前代码形成且每平台不超过 10 个候选。本次 I-14 没有重新访问平台，而是
重新调用当前根项目 `verify_image_artifacts()`，重新读取所有 manifest 和 staging 文件，并逐文件
验证受控路径、SHA、真实 MIME、后缀、尺寸和解码；这避免新增平台副作用，同时证明保存证据没有
损坏。

| 平台 | 完成帖 | manifest 行 | 重新验证文件 | 角色 | 来源键 | 音视频文件 | 结果 |
|---|---:|---:|---:|---|---|---:|---|
| B站 | 1 | 3 | 3 | `content` | `image_urls` | 0 | 通过 |
| 微博 | 1 | 14 | 14 | `content` | `image_list` | 0 | 通过 |
| 抖音 | 1 | 4 | 4 | `content` | `note_download_url` | 0 | 通过 |
| 知乎 | 1 | 14 | 14 | `content` | `image_list` | 0 | 通过 |
| 小红书 | 1 | 6 | 6 | `content` | `image_list` | 0 | 通过 |

合计 5 帖、41 个 manifest 行、41 个文件、41 次根项目字节复验；全部满足
`candidate_posts == complete_posts`、
`expected_images == downloaded_images == validated_images`，失败计数为 0，
`promotion_required=false`、`promoted_images=reused_images=0`。五个 manifest 没有头像、封面、视频、
音乐或知乎公式来源。

XHS 顶层状态仍为 `completed`，冻结计划
`local_image_storage_required=true`、child 命令包含正式图片参数；`artifacts_verified=completed`，
`persistence_verified=skipped(reason=no_import)`，`task_finalized=completed`；checkpoint 也明确
`discovery.skipped(reason=no_import)`。这只是诊断样本通过，不冒充正式数量入库。

## 5. 默认数据与既有文件全量不变量

I-14 对默认库中全部 17,150 个非空本地路径逐条重新读取文件并验证边界和 SHA，不只比较数据库
计数：

| 项目 | I-00 | I-14 | 结论 |
|---|---:|---:|---|
| 数据库 SHA-256 | `a7f78d3636025d61b442b50050afb2c36b7808772131d77df1c344bcdf371006` | 相同 | 未改默认库 |
| 数据库大小 | 143,273,984 bytes | 143,273,984 bytes | 相同 |
| `web_posts` | 6,719 | 6,719 | 相同 |
| `web_post_images` | 63,477 | 63,477 | 相同 |
| 非空 / 唯一本地路径 | 17,150 / 17,150 | 17,150 / 17,150 | 相同 |
| 本地文件缺失 | 0 | 0 | 通过 |
| 文件 SHA 不一致 | 0 | 0 | 通过 |
| 绝对路径 / 路径逃逸 | 0 / 0 | 0 / 0 | 通过 |
| `data/media` 文件 | 0 | 0 | 未晋升历史数据 |
| `PRAGMA quick_check` | `ok` | `ok` | 通过 |
| 外键违规 | 0 | 0 | 通过 |

平台记录数仍是 B站 3,006、微博 1,007、抖音 461、知乎 437、小红书 1,808。主程序阶段没有运行
`scripts/backfill_local_image_paths.py` 的 apply，也不存在对默认库的历史图片补全写入。

## 6. 完成定义核对

- [x] I-00 至 I-13 无跳步并各自有验收/提交。
- [x] 五平台显式正文图投影替代递归 URL 提取，头像等噪声在下载前过滤。
- [x] 五平台会话下载、manifest、根复验、长期晋升和 SQLite 写回已实现。
- [x] 正式 runner 本地图片门禁生效，URL-only 无法完成。
- [x] 临时 SQLite/媒体根、单元、平台专项和真实样本全部通过。
- [x] 视频、音乐、封面、作者主页、头像和公式图未进入正文图下载链路。
- [x] 正式契约、架构、持久化、平台和运维文档与冻结哈希一致。
- [x] 默认库和现有历史文件从 I-00 起保持不变。
- [x] 本报告明确记录 `MAIN_PROGRAM_READY=true`。

## 7. 阶段边界

主程序阶段到此结束。允许下一阶段开始 H-00“重新盘点并冻结历史输入”，但只有在新的明确执行
请求下才可继续；本次任务按用户要求停在主程序阶段，不自动开始当前数据补全。
