# 图片本地存储主程序 I-00 基线报告

生成时间：2026-08-07T17:06:02+08:00

本报告冻结
`2026-08-07-multiplatform-local-image-storage.md` 阶段 I 开始前的实现、默认数据库和既有图片状态。
这些数量只用于阶段 I 不变量校验；阶段 II 必须在 H-00 重新只读盘点，不得直接沿用本报告。

## 版本与工作区

| 项目 | 值 |
|---|---|
| 根项目起始 HEAD | `b28195f9a6d8070a0442f2a2e673395623f104b5` |
| MediaCrawler HEAD | `78a2b8aa9058112b454fe86ce08165773d45501a` |
| 根项目工作区 | clean |
| MediaCrawler 工作区 | clean |
| Python | `3.12.13` |
| uv | `0.11.19` (`7b2cff1c3`, `aarch64-apple-darwin`) |
| 冻结文件校验 | passed |

## 默认数据库不变量

默认数据库：`data/trippostcollect.sqlite`

| 项目 | 值 |
|---|---:|
| 文件大小 | 143,273,984 bytes |
| SHA-256 | `a7f78d3636025d61b442b50050afb2c36b7808772131d77df1c344bcdf371006` |
| `PRAGMA quick_check` | `ok` |
| `PRAGMA foreign_key_check` 违规数 | 0 |

当前关系数量如下。`content images` 是数据库当前关系，不是 I-01 显式投影后的权威数量。

| 平台 | 帖子 | content images | 带本地路径的 content images | author avatars |
|---|---:|---:|---:|---:|
| bilibili | 3,006 | 18,050 | 0 | 0 |
| weibo | 1,007 | 6,384 | 0 | 0 |
| douyin | 461 | 4,991 | 0 | 0 |
| zhihu | 437 | 10,767 | 0 | 437 |
| xhs | 1,808 | 21,040 | 17,150 | 1,808 |

I-01 只读权威投影的验收目标分别为 B站 18,050、微博 6,384、抖音 4,528、知乎 10,765、
小红书 17,416。当前关系与权威投影的差异留到 `MAIN_PROGRAM_READY=true` 之后的 H 阶段处理。

## 既有本地图片不变量

| 项目 | 值 |
|---|---:|
| 带本地路径的关系行 | 17,150 |
| 缺失文件 | 0 |
| 数据库 SHA 与文件 SHA 不一致 | 0 |
| 排序清单 SHA-256 | `a6031e2401bdfe224fe98835ff4726a1bac6f0cbfdbe85cb8ef4d3cb06c44ad5` |

排序清单摘要逐行包含平台、帖子 ID、角色、序号、项目相对路径、数据库文件元数据和实际文件
SHA-256；这里只保存汇总摘要，不复制图片或敏感来源 URL。

## 临时根与容量

阶段 I 初始临时根：`temp/image-main-program-i00.9Ko4O5`

- 临时根位于项目 `temp/` 下并已创建。
- 临时根不等于默认数据库路径。
- 临时根不等于计划长期媒体根 `data/media/`。
- 项目卷可用空间：247 GiB（基线采样值）。

阶段 I 的 SQLite、staging、manifest 和媒体文件测试只能使用独立临时路径或 `--no-import`；不得
以此临时根替换默认数据库，也不得在 I-14 前运行任何 H 阶段历史 apply。

## I-00 结论

`I_00_BASELINE_READY=true`

本步骤只进行了只读数据库/文件校验并创建空的测试临时目录；未修改默认数据库、配置、浏览器
状态、现有图片或 MediaCrawler 工作区。
