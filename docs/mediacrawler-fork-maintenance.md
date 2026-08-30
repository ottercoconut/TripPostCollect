# MediaCrawler 本地改造与上游同步

`tools/MediaCrawler/` 是独立 Git 仓库，不是根项目的 submodule。根项目提交不会记录其 HEAD；发布、
回滚和合并必须分别处理两个仓库。`origin` 指向开源上游 `NanmiCoder/MediaCrawler`，本地长期改造用于
TripPostCollect 的授权、低频、图文限定、证据保存和 SQLite 入库流程，不应直接推送到上游 `main`。

## 当前同步基线

- 2026-08-20 已获取并合入上游 `origin/main` 的 `d6f7c5b`。
- 本地 MediaCrawler `main` 与 `codex/generic-post-detail-repair` 均位于整合提交 `b6cbba2`。
- 本次上游带入 B站评论分页修复、XHS 原始响应测试和 README 资源更新；XHS 冲突按下述本地边界处理。

提交哈希只是本次审计锚点。以后同步前必须重新 `git fetch origin main`，不能把这里的哈希当成永久上游。

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
