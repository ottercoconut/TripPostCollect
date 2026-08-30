# 页面证据平台

固定 URL 页面任务由 `ctf_resource_crawl.py` 生成逐页证据，并由
`import_ctf_captures.py` 归一化。

当前 `config/crawl_targets.json` 没有 `ctf_resource_crawl` 正式任务。直接运行执行器仅用于
开发或诊断验证，使用独立浏览器 profile，且不由 `login_warmup.py --targets all` 验证。
以后若新增固定 URL 正式任务，必须在配置中声明 `job_kind=ctf_resource_crawl`，并从
`crawl_runner.py` 进入；runner 状态、冻结阶段、页面证据和导入结果全部成功后，才算该 URL 完成。

- 单个显式 URL 成功仍只代表该 URL 完成，不能汇报为平台批量来源耗尽。
- 页面错误、搜索页、中间页和验证码页只保留证据，不生成用户内容记录。
- 页面没有明确平台发布时间时保持 NULL，不得用抓取时间代替。
- 页面元数据在写入 JSON/SQLite 前递归清除头像字段及同记录内经这些字段证明的重复 URL。
- 任意图片响应无法证明是正文或必要证据角色，因此执行器只保存不含 URL 的聚合计数，不读取或保存
  图片响应体；`images.json` 与 `failed_images.json` 固定为空数组。截图属于整页证据附件，即使画面
  中出现头像，也不得把它拆分、登记或提供为头像/正文图片。
- 导入器不读取历史 `images.json` 创建 `ctf_capture_images` 或 `web_post_images`；更新旧页面证据时
  删除既有未分类图片行。以后只有新增明确角色证明和相应治理契约后，才可重新启用 `page` 关系。
- `--keyword` 只用于记录检索主题，可以省略，也可以使用“崂山攻略”等不含“青岛”字样的青岛
  主题词。执行器和导入器不做关键词硬门禁；固定 URL 与声明主题是否一致由操作人确认。
