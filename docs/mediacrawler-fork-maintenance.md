# MediaCrawler 本地改造与上游同步

`tools/MediaCrawler/` 是根项目的 submodule，gitlink 钉住当前配套提交，clone 时用
`git clone --recurse-submodules` 或 `git submodule update --init` 取得可运行完整状态。它仍是独立
Git 历史：发布、回滚和合并必须分别处理两个仓库。其 `origin` 指向开源上游
`NanmiCoder/MediaCrawler`，`fork` 指向个人备份 `ottercoconut/MediaCrawler`；本地长期改造用于
TripPostCollect 的授权、低频、图文限定、证据保存和 SQLite 入库流程，不应推送到上游 `main`。

日常分支清理、配套版本记录和 Git 备份恢复见[本地分支与版本管理](version-control.md)。

## 2026-08-20 上游同步记录

- 2026-08-20 已获取并合入上游 `origin/main` 的 `d6f7c5b`。
- 当时本地 MediaCrawler `main` 与 `codex/generic-post-detail-repair` 均位于整合提交 `b6cbba2`。
- 本次上游带入 B站评论分页修复、XHS 原始响应测试和 README 资源更新；XHS 冲突按下述本地边界处理。

提交哈希只是本次审计锚点。以后同步前必须重新 `git fetch origin main`，不能把这里的哈希当成永久上游。

## 2026-09-26 知乎正文段落提取与 main 收敛

- MediaCrawler `main` 快进至 `2bcde689cc9a1b50cbcc7255597e9da1ff1a1b30`：相对旧配对
  `d61b6a34bf2451edda6f13ea4e1da312215415eb` 仅新增一个知乎提交。answer/article 正文改用
  `extract_zhihu_content_text`，保留块级换行并剔除 figure/figcaption；`image_list` 仍从原始
  HTML 提取，`title`/`desc` 保持扁平提取。根项目不改代码，继续直接消费 child 的
  `content_text`。
- 新行为使未来知乎记录的 `content_text` 与 `content_length` 不再包含图片说明文字，边界样本的
  `topic_relevant` 可能变化；存量记录保留旧格式，走历史详情修复时自然升级。
- 分支收敛：MediaCrawler 本地仅剩 `main`，7 个已整合的 codex 分支删除，引用清单、bundle 与
  `audit.json` 保存在 `.git/branch-cleanup/20260926-004707/`；根仓库此前已在
  `20260925-153601-root-consolidation` 收敛。
- 验证：MediaCrawler 自身 `.venv` 设置 `PYTHONPATH=../../src` 后 `pytest tests -q` 478 项通过；
  根项目 `python -m pytest` 与 `python scripts/verify_frozen_files.py` 通过。本条目所在根项目
  提交即根侧配套记录。

## 2026-09-09 小红书恢复版本

该次是本地恢复逻辑修复，没有同步上游。当时两个仓库使用分支 `codex/xhs-checkpoint-empty-batches`；
分支清理后按以下提交配对追溯：

- MediaCrawler 配套提交 `d61b6a34bf2451edda6f13ea4e1da312215415eb`：登录失效留在原抓取页，
  作者辅助页不再接管为主页面。
- 根项目配套提交 `4a7a95d02edaf66cb57320c47eceac551fe5f3b9` 保存逐批 checkpoint、空产物批次握手和
  精确保存失败原因；具体行为见
  [小红书批次恢复点](platforms/xhs.md#批次恢复点)。两个仓库需分别保留对应提交，根仓库不会
  自动记录或恢复 MediaCrawler HEAD。
- 验证：根项目 `python -m pytest -q` 为 800 项通过；MediaCrawler 在自身虚拟环境设置
  `PYTHONPATH=../../src` 后运行 `python -m pytest tests -q`，478 项通过。根项目相关文件 Ruff、
  Python 编译和 `python scripts/verify_frozen_files.py` 均通过。
- 同账号同配置正式 runner 的 dry-run 已验证，读取 `xhs-a01` 的第 31 页深层边界，后四阶段保持
  `frozen`。本次未重新启动正式抓取；测试与 dry-run 不代表平台来源耗尽。

提交不包含工作区中独立的通用调度恢复和知乎修改，也不包含运行产物、SQLite 或临时登录状态。

## 不得被上游覆盖的本地边界

- HTTP 401/403、429、平台安全限制、登录失效、验证码、账号/IP 封禁和浏览器整体失败属于运行级
  阻断；必须保留结构化错误码并停止当前轮次，不能降级成跳过单帖或作者后继续。
- 只保存图文正文、研究所需作者字段和正文图片；视频记录、视频媒体和作者头像不得进入项目产物。
- 正文必须有平台详情证据；搜索摘要、ID-only 空壳和其他实体不能补签 `detail_observed`。
- 图片必须经过有限重试、manifest、字节校验和候选级失败隔离；正式写入仍由根项目负责晋升和事务。
- 通用平台必须产生 `social_high_risk` 行为与策略证据；小红书继续使用独立账号、租约和
  `xhs_guarded` 流程。
- TripPostCollect 的自适应分页、候选记忆、详情修复环境变量和 JSONL 字段属于根项目接口，不能在
  上游同步时静默删除或改名。

## 同步步骤

1. 在 MediaCrawler 功能分支获取 `origin/main`，分别记录共同基点、上游独有提交、本地独有提交和
   最终文件树差异；不要直接在本地 `main` 上试合并。
2. 用三方合并识别双方同时修改的文件。平台 client/core/exception 冲突必须按根项目文档和测试逐项
   决定，不能机械选择 ours 或 theirs。
3. 原样吸收不影响正式契约的上游修复；语义重叠的改动保留本地结构化失败类型，并把上游新增测试
   改写为本项目契约的断言。
4. 在 MediaCrawler 自身 `.venv` 中运行测试；Redis、MongoDB 等外部依赖不可用时，分别报告环境跳过
   或失败，不能把排除后的套件冒充完整全绿。
5. 在根项目 `.venv` 中运行受影响文件的 Ruff、编译、根项目全量测试和冻结文件校验。
6. 通过根项目 `scripts/mediacrawler_crawl.py --no-import` 做青岛相关、超时边界明确、媒体根位于
   `temp/` 的真实低频诊断。诊断不写 SQLite、不推进 checkpoint，也不作为正式抓取完成证据。
7. 所有门禁通过后，先快进 MediaCrawler 本地 `main`，再提交和快进根项目 `main`；不得只合并其中
   一个仓库。

上游同步后的正式抓取、登录、修复和入库仍分别以
[正式抓取执行契约](formal-crawl-contract.md)、[运行手册](operations-runbook.md)和各平台文档为准。
