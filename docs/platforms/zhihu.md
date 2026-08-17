# 知乎

知乎正式抓取服从[正式抓取执行契约](../formal-crawl-contract.md)。本文只维护知乎的登录、详情、字段和
图片资产差异。

## 入口与类型

- 正式入口：`crawl_runner.py` 调用 MediaCrawler 知乎搜索。
- 登录态必须具有经关闭重开复验的 `d_c0/z_c0` Cookie snapshot。
- 只接受 answer 和 article；zvideo 跳过。
- 去重键为内容 ID，answer URL 同时保留 question ID。
- 行为阶段必须在本轮真实关键词搜索页完成；页面导航超时可以作为软失败继续检查已验证 Cookie 和
  API，最终仍以 API、字段和阻断证据决定结果。

## 正文、作者与详情

- 正文已有非空完整 `content` 和正文图时使用 `content_detail_source=search_content`。
- 搜索结果缺正文或图片时，必须低频请求详情并使用 `answer_detail` 或 `article_detail`。
- 详情只有在 HTTP 成功、`js-initialData` 能解析目标 entity 且形成非空内容对象时，才写
  `content_detail_status=detail_observed`。详情观察后仍无图片才是 `missing_content_image`；
  `request_failed`、`parse_failed` 和旧产物缺状态都属于 `content_detail_unobserved`。
- `title`、`desc` 和 `excerpt` 不能替代正文。
- 作者粉丝来自搜索 author/member 的 `follower_count`，要求
  `followers_observed=true`、`author_followers_source=search_author`；缺失不能补 0。
- 详情候选级失败有限重试后写 `candidate_skipped`；登录、授权、频控和验证仍是运行级阻断。

## 正文图片与资产键

正文图只来自最终 answer/article HTML 归一后的 `image_list`。`zvideo`、`/equation` 公式图、头像、
作者主页、封面和搜索预览在候选阶段排除。

同帖 `zhimg.com` URL 按稳定资源路径移除 `_r`、`_<width>w` 等已知变换后缀，保留首次 URL 后再下载；
原始 `image_list` 保留所有 URL 证据。该规则不作用于外部域名，不使用感知哈希或视觉相似度。平台层
复用当前登录会话写 staging/manifest，根项目再执行通用字节复验、同帖 SHA-256 去重、晋升和事务。

历史 manifest 中旧 zhimg URL 变体的严格兼容条件见[数据持久化](../data-persistence.md)；它只用于
恢复复验，不改变当前候选投影。

## 发现与诊断

有 checkpoint 时先刷新顶部，再从保存页码继续；完整页保存下一页，页中途停止保留当前页。正常
workflow 不手工传 `--start-page` 或 `--resume-summary`。

连续停滞使用 `stagnation_basis=valid_new`：只有批次没有新增满足正式字段 profile、且数据库中不存在
的有效记录时才累计；新无效候选、重复候选和数据库已有记录都不能重置停滞计数。

重新核验一组已知 answer/article URL 时，使用根执行器的
`--zhihu-detail-urls-file <JSON数组文件> --no-import` 诊断模式。它不写正式 checkpoint 或数据库，
也不能作为正式轮次完成证据；不要直接运行第三方内部命令。

需要正式修复历史库中非 `detail_observed` 的既有行时，改用
`scripts/repair_post_details.py --platform zhihu`。总控清单把既有内容 ID 与 answer/article HTTPS URL
严格绑定，detail 结果仍必须取得 `answer_detail` 或 `article_detail`、搜索作者粉丝证据和完整正文图，
再走媒体晋升与 SQLite 事务。该修复入口不写搜索 checkpoint，完成含义只覆盖清单中的旧行。
