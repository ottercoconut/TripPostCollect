# 微博

微博正式抓取服从[正式抓取执行契约](../formal-crawl-contract.md)。本文只维护微博的登录、搜索分页、详情、
作者、字段和图片差异。微博自 #211 起改为桌面站页面取数（L1），并纳入真实访问门禁；`m.weibo.cn` 移动接口、
浏览器外搜索与详情请求、移动详情 `$render_data` 读取已全部删除。

## 入口与登录

- 正式入口：`crawl_runner.py` 经 `mediacrawler_crawl.py` 启动根包 worker（`trippostcollect.platforms.entry`）
  执行微博搜索，实现位于 `src/trippostcollect/platforms/weibo/`。
- 浏览器沿用标准模式持久 profile `data/runtime/platform_sessions/weibo/profile`，与 warmup 共用
  `launch_profile_context`：不强制 User-Agent，原生窗口 1450×900，`zh-CN` 与 `Asia/Shanghai`，沙箱开启，
  不做 viewport 设备模拟（#136、#139）。持久 profile 是登录态的唯一权威来源，worker 不向浏览器注入 Cookie。
- 登录确认只读页面状态：worker 打开 `https://weibo.com/`，经已登记的状态脚本读取 `window.$CONFIG.uid`。
  值存在且为真（非 0 数字或非空字符串）才算已登录，访问账本记为 `page_state`。读不到、为假，或页面落到
  `passport.weibo.com`（含访客页 `visitor`），即以 `runtime_failed/login_required` 运行级阻断结束，不发起
  任何搜索，也不在本轮登录；操作人先运行 `scripts/login_warmup.py --targets weibo` 再重跑。
- warmup 用同一判定：首页 `$CONFIG.uid` 为真即成功；否则把桌面 `passport.weibo.com/sso/signin` 登录页
  置前，人工完成登录后关闭重开 profile，再读一次首页 `$CONFIG.uid`，为真才算成功。
- 行为阶段在本轮关键词的桌面搜索页完成（见“搜索”）。
- 页面落到安全验证（验证码、滑块、“安全验证”一类可见特征）是 `captcha_detected`；可见“访问频繁”
  “操作过于频繁”一类提示是 `rate_limited`。这两组 URL 与可见特征写在平台代码的常量里。截至 #211 侦察
  尚未实际遇到，属于待实测的假设。

## 搜索

- 搜索只读本轮搜索页自身的页面内容。worker 导航到 `https://s.weibo.com/weibo?q=<关键词>&nodup=1`，
  即页面上“查看全部搜索结果”的入口（不省略相似结果），并在该页完成行为阶段。
- 搜索结果由服务端渲染在 HTML 里，页面没有返回结果列表的 JSON 接口。每页经已登记的 DOM 脚本读取
  （访问账本记为 `page_state`），读取以下内容：
  - 卡片：`div.card-wrap[action-type="feed_list_item"][mid]`，`mid` 必须是纯数字；
  - 每张卡片的详情链接：`div.from a` 的 `//weibo.com/{uid}/{mblogid}`；
  - 分页控件 `div.m-page2`：当前页 `li.cur`、`span.page-info`（如“共50页/500条”）、页码链接的最大页，
    以及 `a.next` 是否带 `disabled`。
- 用户卡、热门话题卡等不带 `mid` 的卡片不是候选。
- 翻页：先写一段翻页停顿（账本阶段 `search_page`，随机 1.2–2.8 秒），用 `social_high_risk` 的真人分段滚动到
  页面底部，再真实点击 `div.m-page2 a.next`（账本动作 `click_next`）。页面在同一标签内跳到 `&page=N`，然后
  重新读取，并要求 `li.cur` 正好是本轮从 1 连续数的下一页。不拼接页码地址，也不跳页。
- 搜索访问写入访问账本，每条带以下字段：
  - 连续 `search_page`（从 1 起）；
  - `is_target=true`；
  - `has_more`：`a.next` 未禁用为 true，末页为 false；
  - `search_session`；
  - `filter_signature`：默认模式为 `nodup=1`；时间窗模式为 `nodup=1;timescope=custom:<起>:<止>`。

## 来源耗尽与发现记忆

- 来源耗尽只认搜索页自身的结束标记，以 `source_exhausted` 停止：

  | `stop_detail` | 条件 |
  |---|---|
  | `next_disabled` | 当前页有卡片，且 `div.m-page2 a.next` 带 `disabled` |
  | `single_page` | 第 1 页有卡片，且没有分页控件 |
  | `no_results` | 第 1 页没有分页控件，且页面显示“找到0条结果”或“抱歉，未找到相关结果” |

  `nodup=1` 与时间窗搜索的无结果页显示“抱歉，未找到相关结果。”，同时附带几张推荐微博卡片（#215）。
  这些卡片的结构与搜索结果相同，但与关键词无关。只要出现无结果标记，页面上的卡片一律忽略：不进入候选，
  不打开详情，不登记为搜索条目。不带 `nodup=1` 时的无结果页是“找到0条结果”，没有卡片。

  停止事件带最后一页的 `source_visit_id`，并记录：
  - `source_page_info` 原文；
  - `source_max_page`；
  - `page_cap_reached`：是否停在平台 50 页上限，判定见下文。

  `single_page` 没有分页控件，`source_max_page` 为空，`page_cap_reached` 为 false。
- 其他情况一律写 `runtime_failed/search_page_invalid`，不推断为耗尽：
  - 第 2 页及以后没有分页控件或没有卡片；
  - 第 1 页没有卡片却没有无结果标记；
  - 出现无结果标记的同时有分页控件，或出现在第 2 页及以后；
  - 卡片 `mid` 不是纯数字；
  - 点击下一页后页码没有前进。
- 每个查询最多 50 页（第 50 页的 `a.next` 带 `disabled`）。这是平台的翻页上限，不等于该关键词的内容已经
  取尽。操作人决定（#213）：默认模式与其他平台一致，每轮全量扫描，翻到第 50 页同样按 `next_disabled`
  来源耗尽处理，`page_cap_reached=true` 只用于审计。需要上限之外的结果时，由操作人显式配置时间窗 job，
  见下文“时间窗模式”。
- `page_cap_reached` 在以下任一情况为 true：分页控件读到的最大页为 50；或停在第 50 页且 `a.next`
  带 `disabled`。不足 50 页的时间窗页面可能没有 `page-info`，最大页从分页控件的页码读取。
- 每轮从搜索第一页翻到结束标记。已处理 ID 在打开详情前滑过，包括以下来源：
  - 数据库已有 ID；
  - 累计摘要中的有效 ID；
  - 持久已处理候选；
  - 人工排除；
  - 本轮已见 ID。
- 不按保存页码恢复深层前沿，也不做顶部有限刷新。runner 不把 checkpoint 转成 `--start-page`、
  `--top-refresh-max-pages` 或 `--discovery-source-exhausted`；child 收到这些参数直接以
  `weibo_full_scan_rejects_resume_args` 报错。运行级失败时，未完成的累计摘要仍经 `--resume-summary` 合并，
  下一轮取得结束标记后一次性入库。checkpoint 仍写入本轮最后页码与停止原因，供审计，不作续跑起点。
- 默认 job 的查询指纹仍按平台与关键词计算，#211 之前积累的已处理 ID（同为数字 mid）继续有效；
  时间窗 job 的指纹另含 `time_window`，见下文。旧 checkpoint
  中的深层页码不再使用。
- 批次使用 `stagnation_basis=candidate_identity` 记录诊断计数（本页是否出现未知 mid）；该计数不触发停止。

## 时间窗模式（#217）

默认模式不带时间窗。只有操作人在 job 参数里显式写 `time_window` 时，该 job 才按时间窗搜索。这不是
默认行为，也不会自动生成或拆分时间窗。

- 配置写在 `jobs[].params.time_window`，只含 `start` 与 `end` 两个键：

  ```json
  "params": {
    "platform": "weibo",
    "keyword": "青岛旅游",
    "time_window": {"start": "2025-07-01", "end": "2025-07-08"},
    "required_fields_profile": "image_post_with_followers_v1",
    "followers_policy": "required"
  }
  ```

  - 值的格式为 `YYYY-MM-DD` 或 `YYYY-MM-DD-H`（`H` 为 0–23，不补零也可），按北京时间理解。
  - 日期形式表示当日 0 点。
  - 区间左闭右开，即 `[start, end)`；`start` 必须早于 `end`。
  - 其他格式、缺键、多余键或非字符串值都是配置错误。
- 只有微博接受 `time_window`（平台登记 `supports_time_window`）。其他平台的 job 带这个参数是配置错误：
  runner 在同步配置时和派发前都会拒绝。
- runner 传给 child 的参数：
  - 规范化起止 `--time-window-start` / `--time-window-end`；
  - 配置原值 `--time-window-source`（JSON）；
  - `--time-window-job-key`。

  child 只在微博时接受这些参数，起止必须成对，并用同一解析函数重新校验，原值与规范化起止必须一致。
- 一个 job 对应一个关键词加一个时间窗。要覆盖多个时间段，就配多个 job（通常放在 `config/one_off/` 派生
  配置里，见运行手册“一次性配置”）。
- 搜索入口：

  ```text
  https://s.weibo.com/weibo?q=<关键词>&typeall=1&suball=1&timescope=custom:<起>:<止>&nodup=1
  ```

  起止统一写成小时形式 `YYYY-MM-DD-H`，例如上例为 `custom:2025-07-01-0:2025-07-08-0`。
  job 的 `target_url` 写成同一地址，作为配置记录。行为证据的目标页核对（`target_url_ok`）只检查搜索页
  地址中是否含关键词，不比对 `timescope` 等查询参数。
- 统一用小时形式的原因（2026-11-20 侦察，#217）：
  - 平台的日期形式 `custom:D:D` 包含终点当天，而小时形式的终点不含；只用小时形式才能得到左闭右开。
  - `custom:D-0:(D+1)-0` 与日窗 `custom:D:D` 结果相同。
  - 相邻小时窗、相邻日窗都没有重叠，并集等于大窗口。
  - 加不加 `nodup=1`、`typeall=1&suball=1`，结果与顺序都相同。保留 `nodup=1`，与默认模式的过滤口径一致。
- 时间窗 job 的其余流程与默认模式完全相同：
  - 每轮从第 1 页真实点击翻页到结束标记，来源耗尽三种情形与无结果页处理不变；
  - 详情、作者、图片与访问门禁不变。
- 时间窗内翻满 50 页，同样按 `next_disabled` 来源耗尽处理，并带 `page_cap_reached=true`。窗口内上限之后的
  结果不会被取到；是否把该窗口切细、另配 job，由操作人根据摘要决定。
- `time_window` 是来源参数，计入查询指纹。每个时间窗 job 有独立的 checkpoint 与已处理候选记录；
  没有 `time_window` 的默认 job，指纹与 #211 时完全相同。
- 停止事件、批次诊断、child 运行摘要、runner 结果与 dry-run 计划都记录本 job 的 `time_window`：
  - `start` / `end`：配置原值；
  - `start_normalized` / `end_normalized`：规范化小时形式；
  - `timescope`：地址中的值。

  默认模式不写这个键。
- 去重沿用现有机制，不为时间窗另做：
  - 打开详情前，候选会与该平台全部已入库帖子 ID 比对（不分 job），也与本 job 的已处理候选、本轮已见 ID 比对；
  - 入库按平台与帖子 ID 去重。
  - 只在别的 job 中被跳过、没有入库的帖子（如无图帖、视频），会在新的时间窗 job 中再打开一次详情后跳过，
    不会产生重复数据。

## 详情

- 每条未知候选都在搜索页真实点击卡片的时间链接，在新标签打开 `https://weibo.com/{uid}/{mblogid}`。
  点击前写一段 `search_results` 停顿（随机 1.2–2.8 秒）；新标签最多等 8 秒出现。
- 详情数据是新标签页面自身请求 `/ajax/statuses/show?id=…&isGetLongText=true` 的响应（被动截获，
  `page_response`）。只接受 `idstr`（或 `mid`）与卡片 `mid` 完全一致的那一条，同页评论、推荐等响应里的
  其他微博对象不参与。
- 详情标签停留随机 5–9 秒（账本阶段 `note_detail`），然后关闭，回到搜索页。
- 正文取 `longText.content`，没有该键或为空时取 `text_raw`，结果必须非空；来源为
  `content_detail_source=desktop_detail`，并写 `content_detail_status=detail_observed`。
  - 侦察实例中，带 `longText` 的长文，其 `longText.content` 与 `text_raw` 等长，都是全文。
  - `continue_tag` 只是列表页的截断标记，不参与取值。
  - `isLongText=true` 但没有 `longText` 的实例是多图微博，`text_raw` 已是全文。
  - HTML 形式的 `text` 不使用。
- 转发微博只取本条自身的 `text_raw` / `longText` 与 `pic_infos`，不合并 `retweeted_status` 的正文或图片。
- 发帖时间只来自 `created_at`（如 `Tue Aug 11 11:09:54 +0800 2026`），换算为 Unix 时间戳与上海时区时间。
  缺失或无法解析时，该候选写 `candidate_skipped(error_code=created_at_unparseable)`，不写出记录，搜索继续。
- 互动计数取 `attitudes_count`（点赞）、`comments_count`（评论）、`reposts_count`（转发即分享，经
  `shared_count` 写入 `post_shares_count`，不另存转发列）。缺键为 NULL，不补 0；键上的 0 照记为 0。
  缺任一项按字段 profile 判为不合格。微博没有收藏计数（`favorited` 只表示当前账号是否收藏），
  `post_favorites_count` 为 NULL。
- 有 `page_info.object_type=video`、且没有 `pic_infos` 的视频微博按视频跳过：计为已处理候选，进入候选
  记忆，不写项目 JSONL。
- 去重键为数字 mid（`idstr`）。`note_url` 写成 `https://weibo.com/{uid}/{mblogid}`；`mblogid` 另存，不作为 ID。
- 详情候选级失败按 3 次、间隔 1 秒有限重试，然后写 `candidate_skipped` 并进入已处理记忆。候选级失败包括：
  - 找不到卡片时间链接；
  - 新标签没打开；
  - 等待 15 秒仍截获不到匹配的 `statuses/show`；
  - 正文为空。

  以下是运行级阻断，不写 seen：
  - 详情标签落到登录、访客或安全验证页，或出现频控提示（与搜索页同一组判定常量）；
  - `statuses/show` 返回 401/403（`login_required`）、429（`rate_limited`）或 418/432（`blocked_or_forbidden`）；
  - page/context 被关闭。
- 搜索和详情都不走浏览器外 HTTP，因此不再产生 `network_unreachable`；正文图片下载仍按
  `image_download_retryable`。
- 日志只记录 mid、计数和状态码；不打印整条记录、`statuses/show` 或 `sideUser` 原始响应。头像只在内存中
  用于排除，不进入 `model_dump` 和项目 JSONL。

## 作者

- 作者资料来自同一详情标签页面自身请求的 `/ajax/side/cards/sideUser?id={uid}`（被动截获，`page_response`），
  不另开作者页。侦察中该响应与作者页 `/ajax/profile/info` 的 `user` 逐项一致。只接受 `data.user.id` 与
  `statuses/show` 的 `user.id` 一致的那一条。
- 字段映射：

  | 来源 | 写入 | 说明 |
  |---|---|---|
  | `id` | `creator_hash` 键（作者平台 ID） | 整数归一为字符串 |
  | `screen_name` | 昵称 | 原文 |
  | `followers_count` | 粉丝 | int；`followers_observed=true`、`author_followers_source=detail_side_user`；缺键时只认 `followers_count_str` 为纯数字，“万”字串不换算，视为缺失 |
  | `friends_count` | `author_following_count` | |
  | `statuses_count` | `author_posts_count` | |
  | `description` | `author_description` | |

  `author_profile_url` 写成 `https://weibo.com/u/{uid}`。以上各项缺键都为 NULL，不补 0；键上的 0 照记为 0。
  #211 之前的历史行粉丝来源是 `search_author`，导入与修复校验仍接受；新抓取只写 `detail_side_user`。
- 认证原值 `verified`、`verified_type`、`verified_type_ext`、`verified_reason` 写入 `author_json.weibo_verify`。
- 认证判定（#213，操作人 2026-11-20 决定）。机构与个人只按 `verified_type` 区分，不按类型码细分：

  | 条件 | `author_verified` | `author_verified_text` |
  |---|---|---|
  | `verified=true` 且 `verified_type=0`（个人） | 1 | `verified_reason`（去首尾空白），为空时为 NULL |
  | `verified=true` 且 `verified_type` 为 0、-1 以外的整数（机构） | 1 | `机构认证、<verified_reason>`；说明为空时为 `机构认证` |
  | `verified=true` 且 `verified_type` 为 -1、缺失或不是整数 | 1 | `verified_reason`，不加机构前缀 |
  | `verified=false` | 0 | NULL |
  | 没有 `verified`，或不是布尔值 | NULL | NULL |

  - 侦察中见过的机构类型码为 1、2、3、4、7。5、6、8 等尚未见过的值同样记为机构。将来若出现旧“达人”
    （200/220）等情形，另行研究。
  - 依据：2026-11-20 对 40 位作者的只读侦察（#213 评论）。`verified_type` 为 1–7 的作者，主页认证链接全部
    指向机构蓝 V 认证页；0 与 -1 全部指向个人认证页或申请页。
  - `verified_type_ext` 不区分机构与个人，只影响 V 标颜色：个人 0 为黄 V，1、2 为橙 V；机构 53 为红 V（媒体），
    其余为蓝 V。它不参与判定。
  - 带 `author_json.weibo_verify` 的行可按上表离线重算，不需重访。#211 之前的历史行没有原值，回填见 #213。
- 头像（`avatar_hd`、`avatar_large`、`profile_image_url`）、性别（`gender`）、所在地（`location`）、生日、公司
  和 `status_total_counter` 不采集。
- 截获不到匹配的 `sideUser`，或其中没有用户对象，是候选级失败（`author_profile_missing`）：按 3 次、间隔
  1 秒重新打开详情重试，然后写 `candidate_skipped` 并进入已处理记忆。

## 访问门禁

- 微博已纳入访问门禁（#211），每条入库记录须经 `content_visit_ok`。
- discovery 链为 `search_results → note_detail → creator_profile → images`：

  | 阶段 | 规则 | 来源 | 触发 |
  |---|---|---|---|
  | 搜索 | `weibo.search_cards` | `page_state` | 导航或点击下一页 |
  | 详情 | `weibo.statuses_show` | `page_response` | `click_card` |
  | 作者 | `weibo.side_user` | `page_response` | 与详情同一次触发 |
  | 正文图 | `weibo.content_image` | `cdn_direct` | |

  作者访问不需要单独的点击。它必须满足三点：与详情位于同一标签、晚于该次触发，且 `subject_id` 等于
  `statuses/show` 的作者 ID。
- repair 链为 `note_detail → creator_profile → images`：详情由 `navigate` 打开
  `https://weibo.com/{uid}/{mid}`，作者与图片规则不变。
- 每条入库记录带 `content_visit_refs`：`ledger_id`、`search_results`、`note_detail`、`creator_profile`、`images`。

## 正文图片与资产键

- 正文图只来自本条 `statuses/show` 的 `pic_infos`，按 `pic_ids` 顺序排列；`pic_infos` 里没有的 pid 跳过。
- 每张图依次取第一个存在的规格 URL：`largest`（路径段 `large`，原尺寸）、`mw2000`、`original`
  （`orj1080`）。`thumbnail`、`bmiddle`、`large`（`orj960`）不作为正文图。
- `pic_infos` 的 `type` 只接受 `pic`、`gif` 与 `livephoto`（只取静态图规格），其他类型跳过。
- 头像、用户对象图片、封面、视频缩略图和 `retweeted_status` 的图片不进入正文候选。
- 只有 `mix_media_info`、没有 `pic_infos` 的混合媒体微博没有正文图，按缺图判为不合格。
- 稳定资产键为 `weibo:pid:<pid>`，与 #211 之前一致，历史图片记录可以直接对上。
- 图片请求是登记过的 CDN 直连例外，只请求 `*.sinaimg.cn`：
  - UA 取自浏览器 `navigator.userAgent`（经已登记的状态脚本读取）；
  - `Referer: https://weibo.com/`，并带图片 Accept；
  - 不带 Cookie、Origin 或 Content-Type，不经第三方代理。
- 首选规格失败或超限时，退到下一个规格。每张成功下载的正文图在访问账本以阶段 `note_image`、来源 `cdn_direct`、规则
  `weibo.content_image` 登记状态码、资产键和 sha256，供门禁 R5 与 manifest 对账。
- 平台层写 staging/manifest，根项目再执行通用字节复验、同帖 SHA-256 去重、晋升和事务，见
  [数据持久化](../data-persistence.md)。

## 历史详情修复

- `scripts/repair_post_details.py --platform weibo` 只处理既有、但缺少 `content_detail_status=detail_observed`
  的微博行。清单把既有 mid 与 `https://weibo.com/{作者平台 ID}/{mid}` 严格绑定，没有作者平台 ID 的行拒绝。
- detail 模式先在本轮青岛关键词的桌面搜索页完成行为阶段，再按 repair 链逐条 `navigate` 打开详情页。
  - 截获 `statuses/show` 与 `sideUser`，规则与正式抓取相同，要求目标 mid 一致且正文非空，来源标记为
    `desktop_detail`；
  - 再下载正文图。
- 导航响应 401/403 记 `login_required`，429 记 `rate_limited`；其他非 2xx 视为该条详情不可用。
- 不调用 Playwright `page.request` 等请求接口。该入口不写搜索 checkpoint，完成含义只覆盖清单中的旧行。
- 是否用同一链路为约 3882 行历史微博回填作者简介、关注数、作品数和认证，待操作人在 #213 决定。
