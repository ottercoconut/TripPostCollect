# 页面证据平台

豆瓣小组正式任务通过 `douban_group_crawl.py` 从配置的搜索页发现话题 URL，再复用
`ctf_resource_crawl.py` 生成逐页证据，并由 `import_ctf_captures.py` 归一化。

- 正式任务必须配置 `candidate_hard_limit`、`target_new_posts` 和 `max_stagnant_batches`；搜索页
  发现的唯一话题 URL 才计候选，数据库已有 URL 只计 existing，不计有效新增。
- 话题页具备正文、平台原始发布时间、作者 ID/昵称和页面图片关系后才计有效新增；只有
  `valid_new_count` 和实际 `inserted_rows` 同时达到目标才能汇报平台批量完成。
- 单个显式 URL 成功仍只代表该 URL 完成，不能汇报为平台批量目标完成。
- 页面错误、搜索页、中间页和验证码页只保留证据，不生成用户内容记录。
- 粉丝量策略固定为 `conditional_enrichment`；页面未观察到时保持 NULL，不得虚构数值。
- 豆瓣小组话题 capture 出现可见 people URL 且缺少粉丝证据时，在同一正式轮次复用页面证据链
  抓取作者页；people capture 和话题页的 `conditional_enrichment` 父子引用共同作为来源证据。
- 导入只接受同批次、父 capture 匹配的 people 页结果。明确观察到 `rev_contacts` 数值时保存
  `followers_source=people_page`、`followers_observed=true`；只有当前可见隐私文案才能标
  `privacy_restricted`。其他缺失保持 NULL，不默认填 0，也不使用旧 capture 手工补签。
- `max_image_save` 的现有语义仍是响应样本上限；正文图片全量归属问题继续作为独立遗留项。
