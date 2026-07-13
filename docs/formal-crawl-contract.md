# 正式抓取执行契约

本文定义正式抓取的机器语义。数量、平台参数和必需字段 profile 只在
`config/crawl_targets.json` 中配置；本文不重复平台数值。

## 唯一入口

正式任务只通过 `scripts/crawl_runner.py` 执行。其他入口的定位如下：

- `mediacrawler_crawl.py`：调度器调用的结构化执行器，也可用于 `--no-import` 诊断。
- `mediacrawler_batch_validate.py`：开发期字段验证，不代表正式轮次完成。
- `info_collection_benchmark.py`：性能和容量评估，不代表正式轮次完成。
- `ctf_resource_crawl.py`：固定 URL 页面证据执行器，由调度器调用。

## 数量定义

- `candidate_hard_limit`：本轮允许处理的实际原始候选总数。按实际返回记录计数，
  不是底层请求参数的同义词。
- `target_valid_posts`：本轮必须取得的唯一有效图文数。
- `valid_unique_count`：完成视频过滤、平台 ID 去重、必需字段校验和作者字段补全后
  的记录数。
- `processed_rows`：执行过入库 upsert 的行数，不表示新增数或有效数。
- `inserted_rows` / `updated_rows`：数据库新增和更新数量，必须分别报告。

正式结构化任务只有 `valid_unique_count >= target_valid_posts` 才能进入入库阶段并
标记 `target_met`。不能用退出码、`processed_rows`、少量样本或历史数据库总量替代。

## 有效记录

结构化图文记录至少满足：

- 有平台原始 ID 或规范 URL，且本轮唯一；
- 不是视频记录；
- 有正文或标题；
- 有平台原始发布时间；
- 有作者平台 ID 和作者昵称；
- 有至少一个正文图片 URL；
- 满足任务配置指定的作者粉丝量策略和平台字段 profile。

B站、微博、小红书、抖音、知乎使用 `followers_policy=required`。粉丝量为 `0` 只有在
平台响应明确出现该值且保存了 `followers_observed=true` 时有效；缺失值不得转换为
`0`。不提供粉丝量的平台使用 `followers_policy=ignored`，必须由配置声明，不能由
Agent 临场判断。

粉丝来源同时受平台 profile 限制：B站只接受 `relation_stat`，微博和知乎接受
`search_author`，小红书和抖音只接受 `creator_profile`。抖音搜索作者对象中的占位 0
不能替代作者主页结果。

## 冻结执行状态

每个任务必须在 `data/runtime/crawl_execution_states/<run_id>/<job_key>.json` 生成独立
状态文件。状态文件冻结以下输入及 SHA-256：

- 当前配置文件；
- 本执行契约；
- 任务参数和实际命令。

阶段固定为：

1. `plan_frozen`
2. `command_executed`
3. `artifacts_verified`
4. `persistence_verified`
5. `task_finalized`

进入下一阶段前，程序必须重新读取状态文件，确认上一阶段为 `completed` 或明确
`skipped`，并重新校验冻结输入。任一阶段失败或冻结输入变化，后续阶段保持
`frozen`。Agent 只读取状态和摘要汇报，不得手工跳过、改写或补签阶段。

## 停止状态

- `target_met`：有效唯一图文达到目标。
- `candidate_hard_limit_reached`：实际候选达到硬上限但目标未达成。
- `source_exhausted`：平台明确没有下一页或游标。
- `stagnated`：连续配置页数没有新增有效记录；一批固定表示平台的一页，不提供伪页大小参数。
- `login_required` / `captcha_detected`：登录或验证阻断。
- `runtime_failed`：浏览器或本地运行环境失败。

除 `target_met` 外，其余状态都不能汇报为正式结构化轮次完成。固定 URL 页面任务的
成功只代表该页面证据完成，不代表平台批量目标完成。
