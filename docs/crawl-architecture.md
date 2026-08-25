# 抓取架构

> **受限冻结：** 本文件属于治理基线；普通抓取、排障或顺手同步不得修改，只有用户明确授权治理变更，并同步核验对应代码、测试与关联文档时才允许更新。

本文只描述组件边界和数据流。完成谓词、错误分类和候选记忆语义见
[正式抓取执行契约](formal-crawl-contract.md)，操作步骤见[运行手册](operations-runbook.md)，表结构、
媒体事务和校验见[数据持久化](data-persistence.md)。

## 入口与组件

```text
config/crawl_targets.json
  -> scripts/crawl_runner.py
      -> 通用 SQLite checkpoint / seen / exclusion
      -> data/runtime/crawl_execution_states/<run_id>/<job>.json
      -> scripts/mediacrawler_crawl.py
          -> scripts/crawl_policy.py
          -> scripts/mediacrawler_behavior.py
          -> tools/MediaCrawler 或项目自有 B站 article 分支
          -> 共享头像清除器（失败关闭）
          -> 共享正文投影与主题相关性分类（只读最终 web_posts.content_text + 实际关键词）
          -> JSONL + image_manifest.jsonl + staging 图片
          -> 根项目字段、manifest 和字节复验
          -> data/media + SQLite 批次事务（全部结构有效记录）

config/xhs_pool.json + config/xhs_targets.json
  -> scripts/xhs_runner.py
      -> 明确 --account-id、账号租约和加密 storage state
      -> 独立 XHS checkpoint / seen / campaign
      -> data/runtime/xhs/execution_states/<run_id>/<target>.json
      -> scripts/mediacrawler_crawl.py --platforms xhs --behavior-profile xhs_guarded
      -> 与通用平台相同的根项目复验、媒体晋升和 SQLite 入库层
```

`crawl_runner.py` 是 B站、微博、抖音和知乎的唯一正式入口；`xhs_runner.py` 是小红书唯一正式入口。
runner 负责选择任务、冻结计划、调用 child、验证产物、持久化和生成报告。child 只负责平台会话、
发现、字段补全与 staging，不能独立宣布正式任务完成。

青岛主题由操作人在配置和执行前确认。配置解析不按关键词前缀拒绝启动，也不恢复
`web_posts.city_name`；child 与根执行器共享主题分类纯函数，结构有效记录无论相关性均入库，只有
最终 `web_posts.content_text` 包含“青岛”或实际完整关键词的新增记录推动数量目标。平台原始标题和
清洗前正文不直接参与分类。

## 正式生命周期

通用平台和小红书共用下面的逻辑阶段：

```text
冻结配置与计划
  -> 登录、请求策略与行为证据
  -> 搜索/顶部刷新/深层发现
  -> 已知 ID 前置过滤
  -> 详情、作者和权威正文图补全
  -> 头像清除器（失败关闭）
  -> child JSONL、分页事件、manifest 与 staging
  -> 根项目正式字段和本地图片只读复验
  -> 根项目复算 topic_relevant，相关新增满足数量或确认来源耗尽
  -> 完成模式与运行状态门禁
  -> 长期媒体晋升
  -> SQLite 整批事务
  -> checkpoint / seen / campaign 提交
  -> 最终报告
```

行为或策略证据缺失、平台运行失败、字段失败、图片集合不完整、入库失败都会阻断后续阶段。具体
优先级和 `candidate_skipped`、`runtime_failed` 等稳定语义只由正式契约定义。

## 正文图片数据流

```text
平台详情/正文结构
  -> 显式 ImageCandidate(role=content)
  -> 当前登录/签名会话下载到本轮 staging
  -> 原子追加 image_manifest.jsonl
  -> 根项目按同一投影核对身份和数量
  -> 验证路径、SHA-256、MIME、后缀、尺寸与解码
  -> 同帖按已验证 SHA-256 保留首次来源并连续重编号
  -> 原子晋升 data/media/<platform>/<post>/...
  -> web_posts 与 web_post_images 在同一批次事务提交
```

平台 store 只拥有当前会话下载、staging 和 manifest；根项目拥有路径安全、字节复验、同帖去重、
长期文件和 SQLite。头像、作者主页、搜索预览、封面、视频、音乐及知乎公式图在显式字段投影阶段
就没有进入正文图片链路。平台响应可在内存中含头像字段，但共享导出清除器会在任何项目 JSONL、
摘要或 SQLite 序列化前递归删除已知头像键和同记录内经这些键证明的重复 URL；子进程 stdout/stderr
一旦出现已知头像键则整段替换为审计标记，避免日志或摘要尾部泄漏同一头像 URL。清除失败时不落盘。

正式 runner 固定启用 `--download-images`。`--no-import` 诊断只做到 staging 和只读复验，不晋升
长期文件或写 SQLite；视频 store 与旧 `--get-media` 路径不可达。图片格式、事务等式和失败恢复见
[数据持久化](data-persistence.md)。

固定 URL 页面证据不使用正文图片投影。执行器只记录图片请求的非识别聚合计数，不保存任意图片
响应 URL 或响应体；截图作为整页证据附件保留，但不形成 `ctf_capture_images`、正文图片或 `page`
关系。历史 `images.json` 在恢复导入时也不得重新生成未分类图片行。

## 状态与发现记忆

每个任务的冻结状态固定包含五个业务阶段：

1. `plan_frozen`
2. `command_executed`
3. `artifacts_verified`
4. `persistence_verified`
5. `task_finalized`

runner 在进入下一阶段前重新读取状态并校验冻结输入。dry-run 只完成第一阶段；正式运行必须在前一
阶段完成后才能推进，失败后的阶段保持 `frozen`。

通用控制面使用：

- `crawl_discovery_checkpoints` 保存 job 与查询指纹作用域内的安全深层前沿；
- `crawl_discovery_seen_candidates` 保存已有决定性处理结果的候选 ID，包括主题不相关但结构有效且已
  形成 JSONL 的记录；相关性为 false 不产生 `candidate_skipped`；
- `crawl_discovery_candidate_exclusions` 保存操作人明确授权的精确排除。

小红书使用独立的 `xhs_discovery_checkpoints` 和 `xhs_discovery_seen_candidates`，并额外按人工选择的
账号隔离。两套控制面都在 child 摘要与批次证据形成后才提交；媒体或 SQLite 失败不得提前推进。

存在 checkpoint 时先有限刷新顶部，再从深层前沿继续。顶部刷新不推进深层位置；边界页允许重取，
已知 ID 在详情、作者和媒体请求前过滤。平台 cursor 组成和重新建链规则只写在对应平台文档。

## 平台拓扑

| 平台 | 平台层入口 | 独有组件 | 文档 |
|---|---|---|---|
| B站 article | 项目自有 article 搜索/详情分支 | article API、详情正文门禁 | [B站](platforms/bilibili.md) |
| 微博 | MediaCrawler 搜索 | 移动端登录与长文详情 | [微博](platforms/weibo.md) |
| 抖音 | MediaCrawler 搜索 | 浏览器响应监听、offset/search ID | [抖音](platforms/douyin.md) |
| 知乎 | MediaCrawler 搜索 | answer/article 详情、zhimg 资产键 | [知乎](platforms/zhihu.md) |
| 小红书 | 独立 runner + MediaCrawler | 账号租约、加密状态、标签页保护 | [小红书](platforms/xhs.md) |

平台层产出统一 JSONL、分页事件和图片 manifest，根项目使用同一正式校验和持久化层，避免五套长期
路径、事务或完成判据。

## 页面证据

固定 URL 页面由 `ctf_resource_crawl.py` 写页面级产物，`import_ctf_captures.py` 导入
`ctf_captures`，内容就绪时再归一化到用户内容表；任意图片响应不写 `ctf_capture_images` 或图片关系。
当前正式配置没有页面证据任务，直接运行只用于开发或诊断；以后若配置正式任务，仍必须从
`crawl_runner.py` 进入。详见
[页面证据平台](platforms/page-evidence.md)。

## 辅助入口

- `login_warmup.py`：验证或刷新 B站、微博、抖音和知乎登录态。
- `xhs_accounts.py`、`xhs_login.py`：小红书账号登记、隔离登录和状态复验。
- `mediacrawler_login_warmup.py`：通用登录入口调用的平台实现。
- `info_collection_benchmark.py`：通用平台诊断和容量评估，不是正式完成证据。

辅助入口不创建完整正式阶段，不能替代 runner。
