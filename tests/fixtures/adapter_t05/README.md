# 微博固定旧实现

`manifest.json` 记录 fork 固定提交 `5a68eb5098fcd17308c7fe0b9d53916ae839b303`、
旧桥 E 的根提交和各文件 SHA-256。所有 `.txt` 为对应提交的逐字节源码或原许可，禁止把新实现
另存为对照基线。微博目录、本站 store、图片稳定键、时间转换与旧模型均保留完整原文件及版权头。
共享浏览器、候选、事件、JSONL 与暂存层使用 T02–T04 已完成迁移的实现；对照只替换网络、
浏览器、行为出口、时钟和重试等待，不替换本站解析或共享暂存算法。

根实现的变更标识为 T05，许可沿用 `LICENSE.txt`，包内原许可见
`src/trippostcollect/resources/licenses/MediaCrawler-LICENSE`。测试逐文件核对 manifest 后装载旧包。
每个流程场景保存 `old/trace.json` 与 `new/trace.json`，请求 method、完整 URL、body、headers、
调用顺序、事件和异常均保留；浏览器 stealth 字节以哈希标识，图片和净化 JSONL 原字节保存在
各自 `batch/` 中并逐字节比较。

旧隐私用例的评论出口属于未迁 T12 退出切片，只在测试中执行冻结的原投影函数。
SQLite 用例从冻结旧 ORM 提取列声明和非列属性，拒绝未声明字段后用标准库执行内存库
写入与查询，保留原用例名、数量、字段和隐私断言；根环境不引入 SQLAlchemy。
