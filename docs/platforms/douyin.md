# 抖音

抖音正式抓取服从[正式抓取执行契约](../formal-crawl-contract.md)。本文只维护抖音的登录、搜索分页、详情、作者、
字段和图片差异。抖音自 #222 起改为桌面站页面取数（L1），并纳入真实访问门禁。以下内容已全部删除：

- 浏览器外 httpx 客户端；
- `a_bogus` 签名（`signer.py` 与 `douyin.js`）、`x-tt-argus` 与 `uifid` 处理；
- 把浏览器 Cookie 复制进请求头（`update_cookies`），以及 Cookie 快照；
- `/aweme/v1/web/aweme/detail/` 直连详情和 `/user/profile/other/` 直连作者资料；
- 抖音历史详情修复与作者资料回填。

## 入口与登录

- 正式入口：`crawl_runner.py` 经 `mediacrawler_crawl.py` 启动根包 worker（`trippostcollect.platforms.entry`）
  执行抖音搜索，实现位于 `src/trippostcollect/platforms/douyin/`。
- 浏览器沿用标准模式持久 profile `data/runtime/platform_sessions/douyin/profile`，与 warmup 共用
  `launch_profile_context`（#136、#139）：
  - 沙箱开启，`zh-CN` 与 `Asia/Shanghai`；
  - 不传 UA，不做 viewport 设备模拟，按原生窗口 1450×900 启动；
  - 首页在持久 context 自带的第一页打开。

  持久 profile 是登录态的唯一权威来源。worker 不读取 Cookie 值，也不向浏览器注入 Cookie。
- 登录确认只读页面状态：worker 打开 `https://www.douyin.com/`，经已登记的状态脚本读取
  `localStorage.HasUserLogin`，值为 `"1"` 才算已登录，访问账本记为 `page_state`。读不到或不为 `"1"`，即以
  `runtime_failed/login_required` 运行级阻断结束，不发起任何搜索，也不在本轮登录；操作人先运行
  `scripts/login_warmup.py --targets douyin` 再重跑。
- warmup 用同一判定：首页 `HasUserLogin` 为 `"1"` 即成功；否则等待人工登录，关闭重开 profile 后再读一次。
  抖音不再读取 `LOGIN_STATUS` Cookie，warmup 也不再写抖音 Cookie 快照。
- 风控判定只认可见特征。页面 DOM 里常驻一个隐藏的空验证码容器（2026-11-20 侦察，#222），所以：
  - 验证码或滑块容器（类名或 ID 含 `captcha`、`verify` 域名的 iframe）必须可见才算命中：宽高都不小于 40，
    `display`、`visibility` 未隐藏，`opacity` 不为 0，并且在可视区内；
  - 落到验证域名、可见的验证码容器，或页面出现“验证码”“请完成下列验证”，是 `captcha_detected`；
  - 可见的“操作过于频繁”“请稍后再试”一类提示是 `rate_limited`；
  - 可见的登录面板是 `login_required`。

  这几组判定写在平台代码的常量里。滑块不自动处理（#71 A3）。搜索页每一页在采纳前都复查一次这组可见
  特征（视频页不进详情，不能只靠详情路径复查），详情与作者页同样复查。

## 搜索

- 搜索只消费浏览器在本轮搜索页上自身发出的请求的响应（#124，`page_response`）。worker 导航到
  `https://www.douyin.com/search/<关键词>?type=general`，并在该页完成行为阶段。
- 一个任务只接受一个关键词：页面取数一轮只打开一个搜索页。关键词为空或含逗号是配置错误，以
  `runtime_failed/invalid_search_keywords` 结束，不打开搜索页。
- 首页 offset 0 是进入搜索页时页面发出的 `/aweme/v1/web/general/search/stream/` 响应，后续页由低频可见
  滚轮触发页面自身的 `/aweme/v1/web/general/search/single/` 请求。不点“图文”等筛选标签。
- 同一关键词、offset 和 search ID 有多条响应时，合并去重并优先最新的非 `verify_check` 响应；全部为验证
  信号时按风控失败。
- 每次滚轮前写一段翻页停顿（账本阶段 `search_page`）。连续翻页时目标只比已截获的页深一屏。
- 后续页目标 offset 未进入截获缓存时，最多执行 8 次可见滚轮；首页不滚，按同样的 8 次预算（每次间隔
  约 200 毫秒）轮询截获通道。仍未截获即以 `runtime_failed`、`stop_detail=browser_search_response_missing`
  停止。不猜深页，缺失页的候选不写入已处理集合；不能把“浏览器尚未加载”解释为耗尽。
- 页面上的结果卡片是 `#waterfall_item_{aweme_id}`。卡片里没有 `/note/`、`/video/` 或 `/user/` 链接；
  类型、作者和正文都以截获的搜索响应为准。
- 搜索访问写入访问账本，每条带：
  - 连续 `search_page`（从 1 起）；
  - `is_target=true`；
  - 本页登记的全部 `aweme_id` 与类型（`image` 或 `video`，门禁 R2 只排除 `video`）；
  - `has_more`；
  - `search_session`。

  导航动作 `navigate` 写在打开搜索页（`goto`）之前，一轮只写一次，首页访问的触发是这条导航。行为阶段的
  滚轮可能已经让页面自己加载了后续页：这些页采纳时仍挂在导航上，门禁计为 `frontend_self_loads`，不判失败
  （与知乎相同）。采纳时还不在缓存里的后续页，先写 `search_page` 翻页停顿，再以滚轮前写下的 `scroll` 动作
  为触发。`stream` 与 `single` 共用一条已登记的截获
  规则 `douyin.search_response`（路径模板 `/aweme/v1/web/general/search/<kind>/`），响应体只由截获通道读
  一次，首页分块流在解码时重组。请求 URL 只留在内存里，用于按关键词、offset、search ID 匹配，不写入
  账本。访问行在页面采纳该页之后写入，序号是采纳时分配的，不是截获时的序号；平台代码不另挂
  `page.on("response")`。
- 图文在综合搜索里占比低：2026-11-20 侦察中，前 5 页 49 条结果只有 3 条图文，其余是视频或聚合卡。

### 搜索接口契约

页面自身请求的形状，客户端只校验响应：

- 首页为 `/general/search/stream/`，后续为 `/general/search/single/`，offset 按 10 递增；
- 首页流可能保留 HTTP chunk 边界，解析前必须重组全部 chunk；
- 首页响应的 `extra.logid` 作为本轮 search ID，后续页始终复用它，不把每页轮换的 `logid` 当作新 cursor；
- 响应带 `extra: null` 时按空 `logid` 处理。

以下情况均为 `runtime_failed`：

- 非成功 `status_code`；
- `data` 不是列表；
- `has_more` 缺失或非法；
- `has_more=true` 但没有下一 `logid`。

`SearchResponseError` 的具体 reason 必须进入停止证据；状态只保存脱敏元数据，不保存完整响应。首页流解码
失败（如 `invalid_stream_chunk_header`）或响应不是 JSON 对象，同样带具体 reason 以 `runtime_failed` 停止，
不当作“没截到”。

截获的搜索响应 HTTP 状态为 401（`login_required`）、403（`blocked_or_forbidden`）、429（`rate_limited`）时，
与作者资料一样按运行级停止，不进入响应校验和耗尽判断。

`search_nil_info.search_nil_type=verify_check` 固定为 `search_verify_check`。`search_nil_type` 只放行空值与
`service_empty`（深层链尾 `data=[]`、`has_more=0` 时出现，属正常翻到底）；其他取值表示搜索受限，按
`runtime_failed`、`stop_detail=search_nil_<取值>` 停止，不能让同一响应的 `has_more=0` 被当成来源耗尽
（#131：曾在首页收到 1 条非作品卡片、`has_more=0`、`unknown`）。首页流合并时保留任一文档的非空标记，
`verify_check` 优先。

### 空首页门禁

新鲜 page 1、offset 0、空 search ID 返回 `data=[]、has_more=false` 时，必须核对当前可见页面：

- 存在 `waterfall_item_*` 或 `.search-result-card`：`empty_api_response_with_visible_results`；
- 没有卡片，也没有明确可见“无结果”：`ambiguous_empty_first_page`；
- 只有页面明确显示无结果：`verified_empty_first_page`，可以证明当前链耗尽。

前两项属于运行失败；下一轮照常从首页重新扫描。核对结果写入执行事件 `douyin_empty_first_page_checked`，
只留分类、可见卡片数、命中的无结果短语和检查错误，不保存页面 URL（路径里含关键词）。

## 每轮全量扫描与 checkpoint

抖音每轮从第 1 页、offset 0、空 search ID 连续翻到平台 `has_more=false`（#124）。search ID 标识一次具体
排序，新进程打开搜索页得到新 search ID，旧 offset 在新链上没有意义，页面也滚不到那么深；因此不跨进程
跳到深层 page/offset/search ID，也没有顶部刷新、前沿重绑定或耗尽后 reseed。

已处理 ID 在打开详情前滑过，包括以下来源：

- 数据库已有 ID；
- 累计摘要中的有效 ID；
- 持久已处理候选；
- 本轮已见 ID。

runner 不把 checkpoint 转成 `--start-page`、`--top-refresh-max-pages` 或 `--discovery-source-exhausted`；child
收到这些参数直接以 `douyin_full_scan_rejects_resume_args` 报错，`--start-offset`、`--start-cursor` 已删除。
运行级失败时的未完成累计摘要仍经 `--resume-summary` 合并，下一轮取得 `has_more=false` 后一次性入库。

checkpoint 仍写入本轮最后的 page、offset、search ID 与停止原因供审计，不作续跑起点：

- 完整批次记录下一 page、`offset + 10` 和本轮 search ID；
- 页中途停止记录当前三元组；
- `data=[]` 且 `has_more=true` 是可继续空批次；
- `has_more=false` 结束本轮（`stop_detail=has_more_false`），新鲜首页仍必须满足上面的空首页门禁；
- checkpoint 为 `exhausted` 只是上一轮的真实证据，下一轮仍从第 1 页全量扫描。

批次继续使用 `stagnation_basis=valid_new` 记录诊断计数：只有批次没有新增满足正式字段 profile、且数据库中
不存在的有效记录时才累计；新无效候选、重复候选和数据库已有记录都不能重置该计数。无论计数多高都继续
分页，不能触发停止或代替来源耗尽。

## 详情

- 只有图文作品打开详情：`aweme_type=68`，或带正文 `images`。视频在搜索响应里就能认出，计入已处理候选，
  不打开详情，不请求作者，不写内容记录，也不下载媒体。
- 每条未知图文候选都在搜索页真实点击卡片 `#waterfall_item_{aweme_id}`：
  - 卡片不在可视区时，先用可见滚轮把它带入可视区；行为阶段可能已把列表滚走，卡片节点不在 DOM 时，先用
    滚轮向上、再向下逐屏寻找（各最多 16 屏），都找不到才是 `detail_card_missing`；
  - 点击前写一段 `search_results` 停顿（随机 1.2–2.8 秒，连续性为 `completed`）；
  - 点击卡片后，页面在同一标签打开浮层，地址栏变为 `?modal_id={aweme_id}`。
- 打开浮层后 8 秒内，经已登记的状态脚本读取浮层状态（规则 `douyin.modal_state`，`page_state`）：
  - 地址栏 `modal_id`；
  - 是否存在当前播放项 `[data-e2e="feed-active-video"]`；
  - 连播开关状态。

  `modal_id` 必须等于候选 `aweme_id`，且当前播放项存在，这次读取才登记为详情访问。
- 浮层不另发作品请求：2026-11-20 侦察中，打开浮层不发 `/aweme/v1/web/aweme/detail/`，也没有另外的作品
  页面状态，浮层直接用搜索响应里的数据。因此正文、互动计数、发帖时间和图片都取本轮截获的搜索卡片
  `aweme_info`，`content_detail_source=search_aweme_info`；详情访问是真实打开该作品的证据。
- **连播**：浮层底部的“连播”开关（`[data-e2e="video-player-auto-play"]`）默认开启。开启时图文播完会
  自动跳到下一条，`modal_id` 随之改变。
  - 开关状态只看按钮是否带 `xg-switch-checked` 类；`aria-checked` 始终为 `true`，不能用。
  - 浮层里同时有前后几条作品，各有一枚开关。读取和点击都先取当前播放项 `feed-active-video` 内可见的
    那枚；没有时再取可见、纵坐标在 0–900 之间的开关。开关可能晚于浮层渲染，读不到时先在约 3 秒内复读；
    仍读不到不能当成已关闭，按 `douyin_autoplay_still_on` 运行级停止。
  - 每轮第一次打开浮层时，开关若开启，就真实点击一次关闭（账本动作 `toggle_autoplay`），在约 1.2 秒内
    复读，必须已关闭。仍开启是运行级阻断：`runtime_failed`，`stop_detail=douyin_autoplay_still_on`。
  - 关闭后在本轮后续浮层里保持关闭。之后每次读取浮层状态，开关仍须为关闭；若重新开启，按上一条
    再关一次。
- 浮层停留随机 5–9 秒（账本阶段 `note_detail`），然后打开作者（见下文）。
- 关闭浮层：Esc 无效，`[data-e2e="video-close"]` 也点不掉。点击左上角的圆形关闭按钮（账本动作 `close_overlay`）。
  侦察中 `elementFromPoint(67, 67)` 命中的是一个 18×18 的图标，它的父级是 64×64、`cursor: pointer` 的控件，中心
  正是 (67, 67)，点这个父级才能关闭。所以按几何条件选取：视口左上 140×140 内、宽高 24–88、接近正方形、
  `cursor: pointer`、包围盒盖住 (67, 67) 的可见控件，取中心离该点最近的一个；没有找到或没有真正点到时不写
  `close_overlay`。打开过作者侧栏时，第一次点击只是从侧栏返回，要再点一次才关闭。每次点击后在约 1.5 秒内
  轮询，地址栏不再带 `modal_id` 才算关闭。连续 3 次点击仍未关闭，是
  运行级阻断：`runtime_failed`，`stop_detail=douyin_modal_close_failed`。浮层没关会挡住后续卡片的点击，
  不能带着它继续。
- 正文与字段：
  - 只有 `aweme_id` 与候选完全一致、是图文、`desc` 非空、带 `create_time` 和 `author.uid` 时，才写
    `content_detail_status=detail_observed`，否则写 `detail_unobserved`；`desc` 为空时不用标题或封面
    文字顶替。
  - 发帖时间只来自 `create_time`。
  - 互动计数取 `statistics` 下的点赞、评论、收藏和分享数。
  - 内容记录不写 `cover_url`、`video_download_url`、`music_download_url`。
- 去重键为 `aweme_id`。
- 详情候选级失败按 3 次、间隔 1 秒有限重试，然后写 `candidate_skipped` 并进入已处理记忆。候选级失败包括：
  - 找不到卡片；
  - 点击后 8 秒内 `modal_id` 不等于候选，或没有当前播放项；
  - 在打开作者前，`modal_id` 已经变成别的作品（`douyin_modal_advanced`）。

  以下是运行级阻断，不写 seen：
  - 页面落到登录、验证码或频控（与搜索页同一组判定常量）；
  - 连播关不掉；
  - 浮层关不掉；
  - page/context 被关闭。

## 作者

- 作者资料只认新标签作者页。在浮层里要两步真实点击：
  1. 点击当前播放项内的头像 `[data-e2e="feed-active-video"] [data-e2e="video-avatar"]`（账本动作
     `open_author_panel`），页面右侧打开“TA的作品”侧栏。浮层里同时有前后几条作品，选择器必须限定在
     `feed-active-video` 内。
  2. 点作者前写一段 `creator_profile` 停顿（1–2 秒），再点击侧栏标题处的作者链接（账本动作
     `click_author`）。这个链接是 `a[target="_blank"][href*="/user/"]`，路径段等于卡片 `author.sec_uid`，
     要排除页面顶部当前账号自己的 `/user/self` 链接。侧栏没有稳定的容器选择器。2026-11-23 实测（视口
     1450×777）：`feed-active-video` 盖住整个视口，不能用它的边缘区分侧栏；同作者的 `target=_blank` 链接只有
     播放器头像（`data-e2e="video-avatar"`，纵坐标约 316）和侧栏标题（纵坐标约 63）两条。所以排除带
     `data-e2e="video-avatar"` 的头像链接，只取纵坐标不超过 220 的标题带内的链接。侧栏在点头像后约 1 秒
     出现，在约 5 秒内轮询；仍找不到是 `author_link_missing`。
- 点击后 8 秒内出现新标签；新标签往往先是 `about:blank`，从标签出现起再等最多 8 秒，URL 落到
  `https://www.douyin.com/user/{sec_uid}?…` 才比对。新标签 URL 中的 `sec_uid` 必须等于搜索卡片 `author.sec_uid`。
  资料响应从点击作者之后开始截获，不因等待 URL 而后移。
- 作者数据是作者页自身请求 `/aweme/v1/web/user/profile/other/` 的响应（被动截获，规则 `douyin.user_profile`，
  `page_response`）。只接受 `status_code=0`、`user.sec_uid` 等于目标、`user.uid` 等于卡片 `author.uid` 的那一条。
  同一作者页会出现多条该路径的响应，其中包括当前登录账号自己的资料（2026-11-20 侦察），都不参与。
- 访问账本中，作者访问的 `subject_id` 是资料 `user.uid`。作者页 URL 的 `sec_uid` 与资料 `user.sec_uid`
  一致时，`url_user_id` 记同一 uid；不一致时不登记作者访问。
- 作者标签停留随机 5–9 秒（账本阶段 `author_tab`，不参与 R6）后关闭，回到浮层。点作者前的 1–2 秒
  才是 `creator_profile` 停顿。
- 不用侧栏代替作者页：侧栏有时不发资料请求、只显示“0粉丝”占位，侧栏打开后还会按“顺序连播”切到该作者
  的其他作品（2026-11-20 侦察，4 次中 2 次没有资料请求）。
- 字段（#116、#163，键名与 2026-11-20 侦察一致）：
  - 作者统计全部只读作者页资料的 `user` 节点；搜索卡片作者对象里的统计（含占位 0）一律不用，也不在资料
    响应的其他嵌套对象里查找（#225）。
  - 粉丝读 `follower_count`，该键缺失时才读 `mplatform_followers_count`；`follower_count` 为 0 照记 0，不用
    后备键的非 0 值顶替。来源为 `creator_profile`，`followers_observed=true`。
  - 关注取 `following_count`，作品数取 `aweme_count`，获赞数取 `total_favorited`；缺键为 NULL，不补 0。
  - 作者平台 ID 保存 `author.uid` 原始值（记录字段 `author_platform_id`，入库只写同名列），昵称保存原文；`short_id`、`unique_id`
    （抖音号）、头像和 IP 归属地不写出。
  - 作者主页链接由 `sec_uid` 拼成 `https://www.douyin.com/user/<sec_uid>`，写入 `author_profile_url`：先取
    作品作者对象，缺失时取资料 `user` 节点，只接受字母数字与 `-_.`；`sec_uid` 本身不作为单独键写出。
  - 简介只取资料 `user` 节点的 `signature` 原文，写入 `author_desc`，入库为 `author_description`；空串或
    缺失视为没有简介，不读搜索作者对象里的签名。
- 认证（#163，实测对照主页“认证信息”）同样只读资料 `user` 节点：
  - `author_verified_text` 依次取 `account_cert_info.label_text`、`enterprise_verify_reason`、`custom_verify`
    的非空原文，蓝 V 与黄 V 同样处理；取到文字时 `author_verified=1`。
  - 只有 `account_cert_info` 能解析为对象且没有 `label_text`、另两个字段都明确为空串时，记
    `author_verified=0`。
  - 缺资料、缺字段或无法解析时，两列都为 NULL。
  - `verification_type`（蓝 V 也为 0）与单独出现的 `is_biz_account`（页面不显示认证）不参与判定。
  - 四个字段的原值另记在 `author_verify_evidence`，随 `raw_sample_json` 入库供复核。
  - 实测形态（2026-11-12）：蓝 V 为 `label_style=5`，`label_text` 与 `enterprise_verify_reason` 相同；黄 V
    （如音乐人认证）为 `label_style=3`，`label_text` 是“音乐人、艺术家”，`custom_verify` 与
    `enterprise_verify_reason` 都是空串。
- 作者候选级失败按 3 次、间隔 1 秒重新打开详情重试，然后写 `candidate_skipped` 并进入已处理记忆。
  候选级失败包括：
  - 找不到当前播放项的头像，或侧栏里没有作者链接（`author_link_missing`）；
  - 新标签没打开，或 URL 中的 `sec_uid` 与卡片不一致；
  - 15 秒内截获不到匹配的资料响应（`author_profile_missing`）。

  以下是运行级阻断，不写 seen：
  - 资料响应为 401（`login_required`）；
  - 403（`blocked_or_forbidden`）。被拦截时响应往往不是 JSON，看不到 `ArgusSecurityPlugin` 一类标记，
    所以资料响应只要是 403 就停止；
  - 429（`rate_limited`）；
  - 作者页落到登录、验证码或频控。
- worker 判定有效与导入门禁一致：没有 `creator_profile` 粉丝、正文证据或正文图的候选不算有效，也不下载
  正文图。
- 搜索、详情和作者都不走浏览器外 HTTP，因此不再产生 `network_unreachable`；正文图片下载仍按
  `image_download_retryable`。
- 日志只记录 `aweme_id`、页码、offset、计数、状态码和停止码；不打印关键词、正文（`desc`）、`sec_uid` 原值、
  整条记录或资料原始响应。头像只在内存中用于排除，不进入
  `model_dump` 和项目 JSONL。

## 访问门禁

- 抖音已纳入访问门禁（#222），每条入库记录须经 `content_visit_ok`。
- discovery 链为 `search_results → note_detail → creator_profile → images`：

  | 阶段 | 规则 | 来源 | 触发 |
  |---|---|---|---|
  | 搜索 | `douyin.search_response` | `page_response` | 导航或滚轮 |
  | 详情 | `douyin.modal_state` | `page_state` | `click_card` |
  | 作者 | `douyin.user_profile` | `page_response` | `click_author`（新标签） |
  | 正文图 | `douyin.content_image` | `cdn_direct` | |

  作者访问按通用 R4 核验：由详情内的 `click_author` 触发，该点击的 `from_visit` 是本条详情访问；
  `subject_id` 与 `url_user_id` 都等于记录的作者 uid。
- 抖音没有 repair 链。
- 每条入库记录带 `content_visit_refs`：`ledger_id`、`search_results`、`note_detail`、`creator_profile`、`images`。

## 正文图片与资产键

- 只有确认的图文作品进入图片入口。正文图来自搜索卡片 `aweme_info` 的 `images[]`：每张图取 `url_list`
  最后一个非空地址，按 `images` 顺序排列。`images[].uri` 形成 `douyin:uri:<uri>` 稳定资产键，与 #222 之前
  一致，历史图片记录可以直接对上。
- 封面、视频、音乐、头像和搜索预览地址既不写入内容记录，也不进入 `ImageCandidate`、staging 或 manifest；
  代码不得调用视频或音乐下载。视频、音乐和封面文件计数必须始终为 0，正文图不完整时不能 URL-only 完成。
- 图片请求是登记过的 CDN 直连例外，只请求 `*.douyinpic.com`：
  - UA 取自浏览器 `navigator.userAgent`（经已登记的状态脚本读取）；
  - `Referer: https://www.douyin.com/`，并带图片 Accept；
  - 不带 Cookie，不经第三方代理。

  2026-11-20 侦察：页面自身的正文图请求同样发往 `p3-pc-sign.douyinpic.com`、`p9-pc-sign.douyinpic.com`，
  Referer 为 `https://www.douyin.com/`，多数不带 Cookie。
- 每张成功下载的正文图在访问账本以阶段 `note_image`、来源 `cdn_direct`、规则 `douyin.content_image`
  登记状态码、资产键和 sha256，供门禁 R5 与 manifest 对账。
- 平台层写 staging/manifest，根项目按 `note_download_url` 重建候选，再执行通用字节复验、同帖去重、晋升和
  SQLite 事务，见[数据持久化](../data-persistence.md)。

## 历史数据

- 2026-10 之前入库的搜索路径记录仍标为 `content_detail_source=aweme_detail`，实际正文来自搜索卡片；
  是否改写这批历史标签登记在 #117，不在本平台代码中处理。
- 抖音历史详情修复（`repair_post_details.py --platform douyin`）与作者资料回填
  （`backfill_author_profiles.py --platform douyin`）原先走 httpx，已随 #222 删除。2026-11-20 正式库 1085 行中，
  只有 1 行没有详情证据，2 行缺作者主页链接。页面取数版的修复与回填不急，操作人决定以后另做
  （2026-11-20），跟踪于 #223。
- `/note/{id}` 页面能渲染完整作品，但不发 `/aweme/detail/`，作品数据由服务端写在 `self.__pace_f` 的文本块里，
  格式尚未解开（2026-11-20 侦察）。#223 做修复链时先侦察它。

共享的有限重试、`candidate_skipped`、运行级阻断和媒体失败语义见[正式契约](../formal-crawl-contract.md)。
