# 页面证据平台

固定 URL 页面任务由 `ctf_resource_crawl.py` 生成逐页证据，并由
`import_ctf_captures.py` 归一化。

当前 `config/crawl_targets.json` 没有 `ctf_resource_crawl` 正式任务。直接运行执行器仅用于
开发或诊断验证，使用独立浏览器 profile，且不由 `login_warmup.py --targets all` 验证。
以后若新增固定 URL 正式任务，必须在配置中声明 `job_kind=ctf_resource_crawl`，并从
`crawl_runner.py` 进入；runner 状态、冻结阶段、页面证据和导入结果全部成功后，才算该 URL 完成。

- 单个显式 URL 成功仍只代表该 URL 完成，不能汇报为平台批量目标完成。
- 页面错误、搜索页、中间页和验证码页只保留证据，不生成用户内容记录。
- 页面没有明确平台发布时间时保持 NULL，不得用抓取时间代替。
- `--keyword` 必填，且去除首尾空白后必须以“青岛”或“崂山”开头；否则执行器在访问页面前拒绝，导入器也不会把
  该 capture 写入 SQLite。固定 URL 与声明主题是否一致仍由操作人确认，不能靠虚假关键词绕过范围。
- `max_image_save` 的现有语义仍是响应样本上限；正文图片全量归属问题继续作为独立遗留项。
