# 微博

- 持久化登录先在后台移动页验证 `/api/config`；失效时另开并置前
  `passport.weibo.com/sso/signin` 桌面 SSO 登录页。检测到 SSO 登录后再导航到移动端刷新
  Cookie；关闭重开 profile 后必须同时满足 `login=true` 和有效 `uid`。不要在无登录入口
  的移动验证页等待人工操作，也不要把桌面域 `WBPSESS` 单独视为移动端登录成功。
- 登录确认后先进入本轮真实关键词的移动搜索页，再执行共享行为阶段并刷新移动端 cookie；
  行为证据 URL 不含本轮关键词，或行为与请求策略证据缺失时，不得入库。

- 正式入口：`crawl_runner.py` 调用 MediaCrawler 微博搜索。
- 完成模式服从正式契约：正常默认使用 `target-new-posts`；只有用户明确要求某一轮直到来源耗尽时，
  才在该轮命令临时使用 `source-exhausted`，不修改长期配置。
- 粉丝来源：搜索结果 `mblog.user` 的 `followers_count/fans_count` 系列字段。
- 正文来源：非长文使用完整搜索 `mblog.text`，保存
  `content_detail_status=detail_observed` 和 `content_detail_source=search_mblog_complete`；
  `isLongText=true` 必须请求移动端详情并保存 `content_detail_source=mobile_detail`。
  详情失败记录 `full_text_request_failed`，阻断当前批次并保留原页；截断文本不写 JSONL、
  不入库、不进入已处理候选记忆。
- 图片来源：`mblog.pics`；没有正文图片的记录不是有效图文。
- 去重键：微博 ID。
- 粉丝量为 0 时，只有原始 user 对象明确包含粉丝字段才有效。
- 搜索结果数量按实际内容记录计入候选硬上限。
- 默认模式下，综合搜索的停滞按是否出现不在数据库、累计摘要、`crawl_discovery_seen_candidates` 和本 child 已见集合中的微博 ID 计算，不按有效图文数计算。纯文本和视频仍是无效
  图文，但只要页面持续提供新 ID，就继续扫描至有效新增目标、候选硬上限或明确空页；连续配置
  批次没有新 ID 才记为 `stagnated`。批次证据中的 `stagnation_basis=candidate_identity` 必须与此一致。
- 有 checkpoint 时先刷新配置的顶部页，再从保存页码继续。已知微博 ID 在批量全文请求、图片
  和存储前过滤；完整页保存下一页，页面中途停止则保存当前页。顶部刷新不推进深层页码。
- 未达标摘要由 runner 自动累计；正常 workflow 不需要人工传 `--start-page` 或
  `--resume-summary`。明确空页后 checkpoint 标记耗尽，后续只刷新顶部。

## 正文图片本地化

微博只把原始 `mblog.pics` 归一化后的 `image_list` 当作权威正文图，来源字段固定为
`source_key=image_list`，每项角色固定为 `content`。有 `pid` 时以 `weibo:pid:<pid>` 作为稳定资产
键，没有 pid 才回退到规范 URL 哈希。`profile_image_url`、avatar、用户对象中的图、封面和视频缩略
图不会进入候选或下载请求；“自动忽略头像”发生在显式字段投影阶段，不靠图片尺寸判断。

图片字节使用当前移动搜索会话的 Cookie/请求头逐帖下载；整帖全部图片先通过真实格式和解码检查，
再原子写入本轮 `<platform_artifact>/data/weibo/images/<weibo_id>/<index>.<real_ext>`，并更新
`<platform_artifact>/data/weibo/image_manifest.jsonl`。空响应或超时按单图最多 3 次、1–2 秒随机基数
指数退避重试；日志逐次记录重试，成功或最终失败的 manifest 都记录实际 `attempts`。三次仍失败才使用
`image_download_retryable`，写 `candidate_deferred` 后暂时跳过整帖并继续本页后续候选。格式、解码、
大小或明确非重试 HTTP 等终态错误写失败 manifest 后立即停止当前页，不写 deferred。失败 ID 不写
持久候选记忆，checkpoint 回到最早失败页；该帖不允许只保留成功图片子集，后续完整帖子继续参与
本轮既定完成条件。
客户端必须保留真实 HTTP 状态；HTTP 200 空字节由共享 helper 继续有限重试，不得折叠成第一次成功。

根执行器重新按 `image_list` 顺序核对 manifest 身份、SHA/MIME/尺寸和 staging 文件。正式 runner
固定开启正文图下载并晋升到 `data/media/weibo/...`，随后在帖子与图片同一 SQLite 事务保存 URL、
`local_path` 和字节元数据。`--no-import` 小样只保留 staging/manifest；任一图片失败时微博任务不能
URL-only 完成。
