# 平台字段覆盖

本文只描述当前正式结构化能力，不定义数量或成功标准。必需字段以任务的
`required_fields_profile` 为准。

| 平台 | 图文/正文来源 | 权威正文图字段 | 本地存储与排除 | 发布时间 | 作者 | 粉丝量 | 互动 | 当前说明 |
|---|---|---|---|---|---|---|---|---|
| B站 article | `article_view_api` 详情正文 | 详情观察后的 `image_urls` | 当前会话 staging → manifest → `data/media`；搜索预览/封面/头像排除 | 结构化 | ID/昵称 | 作者关系统计 | 赞/评/浏览 | 正常正式入口使用详情实现；历史完整性事件已完成修复与清理 |
| 微博 | `search_mblog_complete` / `mobile_detail` | `mblog.pics` → `image_list` | 当前会话 staging → manifest → `data/media`；头像/封面排除 | 结构化 | ID/昵称 | `mblog.user` | 赞/评/转 | 长文详情重试耗尽后记录并跳过，禁止保存截断正文 |
| 小红书 | `note_detail` 非空 `desc` | 笔记详情 `image_list` | 每个图片对象选择一个正文 URL，staging → manifest → `data/media`；头像/视频排除 | 结构化 | ID/昵称/主页 | 登录会话作者主页 | 赞/藏/评/分享 | 详情或作者补全重试耗尽后记录并跳过 |
| 抖音 | `aweme_detail` 非空 `desc` | `note_download_url` | 保留 `images[].uri` 稳定键，staging → manifest → `data/media`；封面/视频/音乐/头像排除 | 结构化 | ID/昵称/主页 | 作者主页 | 赞/藏/评/分享 | 标题和预览文本不能代替正文 |
| 知乎 | `search_content` / `answer_detail` / `article_detail` | 正文/详情 `image_list` | 当前会话 staging → manifest → `data/media`；公式/头像/作者主页/zvideo 排除 | 结构化 | ID/昵称/主页 | search author/member | 赞/评 | 详情重试耗尽后记录并跳过，标题/摘要不能通过 |

五个结构化平台都配置 `followers_policy=required`：数值、来源和
`followers_observed=true` 缺一不可。真实 0 粉丝有效，缺失后由模型默认生成的 0 无效。
抖音和小红书必须使用作者主页来源，不能用搜索结果中的占位 0 通过校验。
B站搜索结果的 `desc` 和 `image_urls` 只是摘要与预览，必须取得
`content_detail_status=detail_observed` 后才能通过正文完整性门禁；候选自身详情失败在有限重试后
记录 `candidate_skipped` 并进入已处理记忆，登录、风控或频控信号仍停止运行且不写 seen。
五平台都必须同时保存 `content_detail_status=detail_observed` 和上表受信任正文来源；
任何标题或搜索摘要回退都不能使正式记录有效。
小红书笔记 `xsec_token` 不能作为作者主页凭据；作者页出现验证、频控或封禁时运行失败，
不能降级成缺粉丝候选继续完成。

字段代码路径和平台限制见 `docs/platforms/`。

发现记忆与字段能力分开：B站、微博、抖音、知乎由通用 runner 持久化查询指纹、深层前沿、
所有已完成处理候选 ID 和有效累计摘要；抖音额外持久化 offset/search ID。小红书由独立 runner 按目标和账号持久化页码、
search ID、深层耗尽状态、已完成处理候选 ID 和累计摘要，并在详情请求前跳过数据库、持久候选、
累计摘要及本轮已知 ID。
任何平台的发现记忆都不放宽本表字段要求。

五个平台的正式新记录都要求 `image_materialization.complete=true`。每个权威正文图必须在
`web_post_images` 保存 `image_role=content`、连续 `image_index`、来源 URL、项目相对
`local_path`、`width/height/mime_type/sha256`，且本地文件通过根项目复验；只有 URL 不算覆盖。
显式投影后的所有候选先完成 manifest 和字节复验，再在同一帖子内按 SHA-256 保留源顺序首次出现项；重复
来源保留在 manifest 和首项别名证据中。该规则不跨帖子，也不合并哈希不同的视觉近似变体。
知乎在此之前仅按 `zhimg.com` 稳定资源路径合并 `_r`、`_<width>w` 等已知 URL 变体，不使用视觉
识别；被合并变体保留在内容原始证据中而不重复下载。
筛除头像等无用资源依赖上表的显式字段投影，不使用递归 URL 扫描或图片尺寸启发式，因此被排除
资源不会触发下载。作者头像字段还会在首方 JSONL、摘要和 SQLite 序列化前递归清除；五个平台均
不由 TripPostCollect 对外提供或持久化头像字段、头像 URL、头像关系或头像文件。
