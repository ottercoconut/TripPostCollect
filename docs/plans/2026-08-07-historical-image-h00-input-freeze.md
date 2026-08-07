# 历史图片 H-00 输入冻结报告

> 结论：H-00 通过，允许进入 H-01；`HISTORICAL_DATA_COMPLETE=false`。
>
> campaign ID：`historical-images-20260807-v1`
>
> 机器可读基线：
> [`2026-08-07-historical-image-h00-input-freeze.json`](2026-08-07-historical-image-h00-input-freeze.json)

## 1. 执行边界

本步骤只读打开默认数据库，使用主程序已经验收的五个平台显式正文图片投影逐帖重算，没有启动
浏览器、访问平台、下载图片、修改 SQLite 或修改已有图片文件。早期工程文档中的容量数字仅用于
对照；从 H-01 起，以本报告和机器可读基线为唯一输入基准。

## 2. 冻结版本与输入

| 项目 | 冻结值 |
|---|---|
| campaign ID | `historical-images-20260807-v1` |
| 默认库 | `data/trippostcollect.sqlite` |
| 输入数据库 SHA-256 | `a7f78d3636025d61b442b50050afb2c36b7808772131d77df1c344bcdf371006` |
| 数据库大小 | 143,273,984 bytes |
| 根项目主程序 commit | `cca9972738beee92cd8fabd9d1444e2e943a5fa1` |
| MediaCrawler commit | `6e7475d4a3501a346ec0eccb592f5598b9b9718b` |
| 权威投影 SHA-256 | `d0c476b21ca745434244e3e4b778273dd00bb78919cd9cd1bec8428b24a8e52b` |

根项目 commit 与 I-14 发布报告一致。数据库 `quick_check=ok`，外键违规为 0；审计前后数据库
SHA-256 相同。

## 3. 五平台实时投影

| 平台 | 帖子 | 当前正文关系 | 权威正文图 | 误分类 | 已有本地文件 | 待补缺口 |
|---|---:|---:|---:|---:|---:|---:|
| B站 | 3,006 | 18,050 | 18,050 | 0 | 0 | 18,050 |
| 微博 | 1,007 | 6,384 | 6,384 | 0 | 0 | 6,384 |
| 小红书 | 1,808 | 21,040 | 17,416 | 3,624 | 17,150 | 266 |
| 抖音 | 461 | 4,991 | 4,528 | 463 | 0 | 4,528 |
| 知乎 | 437 | 10,767 | 10,765 | 2 | 0 | 10,765 |
| **合计** | **6,719** | **61,232** | **57,143** | **4,089** | **17,150** | **39,993** |

各平台权威投影 SHA-256：

- B站：`e2c31bb6c4d0e54110420ee51f34faf901179f8842dfbb6206c8fd38aec174da`
- 微博：`84f9788c54848f7f5aa5ed7dd0a5467a8d9abef01382f5f25b7655c4481ebbc7`
- 小红书：`2ef8e5b14a3d089da25c0d05ad75e3244567c0fb3d1944bf29d1e246d9d20c17`
- 抖音：`90bd15b4d78411f89000994a3fb3b56fb895adc610fbfe272b8c52414615131e`
- 知乎：`da8446e51e6da35c0491441cf13b4059b50b7cb3e3e01b24e23d1acc5997f2e2`

57,143 等于所有帖子显式投影数量之和。现有权威关系没有缺行和重复资源变体；清理对象全部来自
旧通用 URL 扫描造成的误分类：小红书 3,616 条 `creator_profile_json`、4 条 `note_url`、4 条
`author_profile_url`；抖音 461 条 `cover_url`、1 条 `video_download_url`、1 条
`music_download_url`；知乎 2 条 `author_profile_url`。作者头像关系保持 URL 参考角色，不计入正文
图片，也不下载。

## 4. 不变量与现有文件

| 不变量 | 冻结值 |
|---|---|
| `web_posts` 非图片字段 SHA-256 | `51c265efa2cdbd8f4c69588a7ded1ed054be7258f2282a710b657673fb470458` |
| `web_post_images` SHA-256 | `3cbf8b579494a3904fc7953893020784f2b5fcb2aad70da4cf9d81a992453420` |
| 现有本地文件库存 SHA-256 | `6dcdc295809100777f5a29be603b950cef54e019dbb2f9f8dfb436a0430a9c81` |
| 现有本地权威图片 | 17,150 张、4,596,325,862 bytes |
| 缺失文件 | 0 |
| 文件 SHA 不一致 | 0 |

发现记忆表冻结哈希：

- `crawl_discovery_checkpoints`：`34a5b433d5281733ca6a07d4fe7f750653d946aeb0d0f318b46bc59470de0637`
- `crawl_discovery_seen_candidates`：`5ad57355938919cee6f037dc4cef9af13aa6342cc314abb2da5173593136629c`
- `xhs_discovery_checkpoints`：`dded8ebfd10868e4d1af2e8609f410bbbb764f8576a7a02e91aa7fd4e0ccdc82`
- `xhs_discovery_seen_candidates`：`9446934b2666432dcc2d9fc148201d924ad4b004aff4bccd6bac97737d475680`

磁盘当前可用 264,269,946,880 bytes（246.121 GiB）。H-02 仍须按 H-01 副本演练得出的实际新增
容量重新做空间门禁，不能只依赖本次静态容量值。

## 5. 离线运行材料盘点

| 平台 | 离线材料 | H-03 至 H-07 前置判断 |
|---|---|---|
| 小红书 | 账号 `xhs-a01` 为 active；加密状态可解密；19 个 Cookie、1 个 origin；无活动租约 | H-03 必须按小红书预检流程做在线会话验证 |
| B站 | profile 与 Cookie 快照存在，快照可解析且有 Cookie 标记 | H-04 前在线预检 |
| 微博 | profile 与 Cookie 快照存在，快照可解析且有 Cookie 标记 | H-05 前在线预检 |
| 知乎 | profile 与 Cookie 快照存在，快照可解析且有 Cookie 标记 | H-06 前在线预检 |
| 抖音 | profile 与快照存在且可解析，但离线快照没有可靠 Cookie 标记 | H-07 前先重新登录/验证，未通过不得执行 |

本机配置在本次离线审计中没有解析到可用浏览器可执行文件。该结果只表示 H-00 的离线材料状态，
不能据此判断任一平台的实时登录状态；H-03 至 H-07 必须分别完成浏览器定位和在线会话预检。

## 6. 验收结论

- 每个平台都可由当前 `web_posts.raw_record_json` 和显式投影逐帖复算。
- 权威正文图片共 57,143 张，已有本地文件 17,150 张，历史缺口 39,993 张。
- 输入数据库完整，审计前后 SHA 不变；已有图片库存没有缺失或哈希错误。
- campaign ID、输入 SHA、两个代码版本和所有保护性不变量均已冻结。
- H-00 只读边界满足，允许实现 H-01；尚未清理关系、下载图片或补全当前数据。
