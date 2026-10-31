# T14 固化的旧实现预期

T14 删除 fork 子模块 `tools/MediaCrawler`、私有桥 E（`scripts/mediacrawler_export_entrypoint.py`）与
`platforms/_fork_bridge.py` 之前，把各卡“旧实现与根实现逐项对照”中旧侧当场运行的输出固化到这里。
删除之后，根侧用例只运行根实现并与这些文件比较。各组旧侧的独立程度不同，须如实区分：

- **相对独立的旧预期来自冻结 fixture 路径，但只有站点层是冻结的旧代码**：T05/T06/T07/T09 的冻结对照把
  `tests/fixtures/adapter_t0*` 中逐字冻结的旧站点源码（fork 5a68eb5 等）装回原包名执行；其中许多 `.py.txt`
  直接 import 根模块，共享层（图片暂存与 manifest、事件出口等）转发到根实现——例如 `tools/image_manifest.py.txt`
  整体转出根 `artifacts.image_staging`，图片 manifest 字段（sha256、size、宽高、mime、错误码与消息）在新旧两侧
  都由根实现产生。独立性只覆盖站点层的请求、解析、重试、事件次序与产物组织。
- **fork 工厂守卫跑的也是根实现**：T05/T06/T07/T09 的 fork `main.py` 工厂（及 E）在 T14 前已只做薄转发，
  构造出的 crawler 是根类或其子类。这些守卫本身不提供独立性；独立性来自再生成时 `check_legacy` 的
  `_written` 检查——同一预期文件先后由冻结 fixture 路径与 fork 工厂路径写出时必须逐字节相等，再与本目录比较。
  #59 之后 T06 的 fork 工厂守卫（`test_adapter_t06_bridge.py`）跑的根实现已用新 profile 位置，改为与登记偏离后的
  预期比较、不再写出；T06 再生成只由冻结 fixture 路径写出。
- **shared_staging 只有外层胶水是旧代码**：`tests/golden/shared_staging.json` 中的 Git 基线类原文只是
  外层包装，核心（`stage_post_images`、manifest 写入、`AsyncFileWriter`、`MEDIACRAWLER_DIR`）经 fork
  `tools/*` 转发到根实现。其独立性依赖 `_stage_images` / `_jsonl` 中的字面断言（文件数、错误码、同图同哈希、
  失败不留部分目录、头像不落盘等），固化文件只在此之上钉住逐字节结果。
- **T03 不固化**：fork `tools/trippostcollect_adaptive.AdaptiveAccumulator` 是根类子类，只换了默认事件出口，
  仓库中也没有独立旧实现；固化它等于根实现与自身比较。根侧只保留字面断言
  （`tests/test_adapter_t03.py::test_root_injected_event_sequence`），事件字段的独立断言由原名移植的
  `tests/application/test_discovery.py` 承担。

## 来源

`manifest.json` 记录：生成日期、生成命令、fork 提交（`fork_commit`）、生成时根检出的 HEAD（`root_commit`；
`root_commit_note` 由 `write_manifest` 按生成时实际情况写明：HEAD、工作区是否有未提交改动，以及
`git status --porcelain -- src scripts` 是否为空，为空即写“被测 src/、scripts/ 与 root_commit 相同”）、E 文件 sha256、
fork 内全部 `.py` 源的合并 sha256，以及每个文件的 sha256 和产生它的原双轨测试（`source_tests`，可多条旧路径共用
一份预期，见上文 `_written` 检查）。

生成方式（fork/E 仍在时，经本机测试沙箱，DIR 需可写）：

```bash
pytest -p pytest_asyncio.plugin \
  -m t14_legacy_guard \
  --t14-write-legacy-expectations=DIR \
  tests/test_adapter_t05.py tests/test_adapter_t05_bridge.py \
  tests/test_adapter_t06.py tests/test_adapter_t06_bridge.py tests/test_adapter_t07.py \
  tests/test_adapter_t09.py tests/test_shared_staging.py
```

守卫用例（标记 `t14_legacy_guard`）不带该选项时把旧侧当场结果编码后与本目录逐字节比较；带选项时改为
写到 DIR 并写 `DIR/manifest.json`。人工审阅差异后整体替换本目录。编码与归一规则见
`tests/support/legacy_expectations.py`。

## 比较粒度

- 与原对照相同：原对照的归一（小红书 `normalized()`）、有意偏离的替换（#49 作者原值、#52 作者页取数、
  #55 主页关闭异常类名）都在生成旧侧时照原样生效；根侧沿用同一 drive 与 `==` 语义。
- 新增的只有四项，两侧同样处理：dict 按键排序书写（`==` 本与顺序无关，避免目录遍历顺序随 OS 不同）；
  检出根与临时目录换成占位符；超过 16 KiB 的 bytes（stealth 脚本原文）只存 sha256 与长度；
  键名含 `avatar` 的值只存摘要，文件不含头像 URL。
- 测试输入中的 PNG 一律由 `tests/support/stable_png.py` 手工拼出（deflate 存储块，不经压缩器）。Pillow 内置
  zlib-ng 按 CPU 特性选择实现，同一图像在 macOS arm64 与 Linux x86_64 上压缩字节可能不同，曾使 T05/T07/T09
  在 macOS 上与 Linux 生成的预期不一致；改用稳定输入后从旧侧重新生成。JPEG 输入（shared_staging）仍由 Pillow 生成。
- `shared_staging`：根内容出口已无评论/创作者写出（T12 退出切片），根侧比较排除基线中的
  `_comments_`/`_creators_` 文件；根配置在构造时冻结，换目录即构造新 sink。

## T14-C

删除 fork/E 时同批删除守卫用例与只测旧桥的用例（标记 `t14_legacy_guard` / `t14_legacy_only`），
本目录与根侧比较用例保留。之后若根实现有意改变行为，在根侧比较中登记偏离并变换预期，不修改固化文件，也不得用根实现重新生成。已登记：#59（`tests/support/platform_session_deviation.py`：T06 抖音持久 profile 位置、shared_staging 缺省暂存根）；#113（`tests/support/zhihu_login_paging_deviation.py`：T07 知乎不注入快照 Cookie、/me 失败即 login_required、来源耗尽只认 `paging.is_end`、401/403/429 不重试、图片请求不带会话头、API Referer 用本轮检索词）；#114（`tests/support/weibo_fetch_deviation.py`：T05 微博图片直连新浪且不带 Cookie、修复回退删除 Playwright 请求接口）；#124（`tests/support/douyin_full_scan_deviation.py`：T06 抖音搜索只经浏览器截获，首页直接请求删除、后续页 8 次滚轮后的直接请求改为一屏滚轮截获，首页可见卡片重建退出；T11 冻结执行器删除抖音三元组续跑参数、env 与 reseed 证据）；#127（`tests/support/ua_from_browser_deviation.py`：T05 微博浏览器上下文不再强制 iPhone UA、视口改为与 warmup 相同的 1280×900，接口与图片请求头 UA 取自浏览器 `navigator.userAgent`；T07 知乎 CDP 启动不再传写死的 Chrome/128，API 与图片请求头 UA 取自浏览器；T11 冻结执行器删除 `BILIBILI_BROWSER_USER_AGENT` 重导出，B站图片薄包装转发会话 UA）。

已处理：

- 上文“来源”中的生成命令、`--t14-write-legacy-expectations` 选项、守卫用例与 `write_manifest` 已随 fork/E
  在 T14-C 删除；本目录此后不能再生成，生成代码见 Git 历史（`070d5ad`，T14-B2）。
- #71 第一批（小红书取数改为浏览器 L1）后，小红书的搜索、详情、作者、图片与登录态不再经 HTTP 客户端，
  T09 冻结对照失去对照对象：`tests/test_adapter_t09.py`、本目录 `T09/`（36 个文件，连同 `manifest.json` 中的条目）
  与 `tests/fixtures/adapter_t09/` 整体退役删除，不做偏离登记；非旧实现对照的断言已迁到
  `tests/platforms/xhs/` 下对应用例。历史内容见 Git。
- #114（微博请求失败分类、登录复查、删除 L3 回退）后，T05 中语义有意改变的 14 个固化文件连同
  `manifest.json` 条目删除：`frozen_implementation/` 下 `search_retry`、`search_error`、`login_expired`、
  `detail_403`、`detail_429` 的 repair0/repair1，以及 `repair_api-repair1`、`repair_rate_limit-repair1`；
  `repair_exception_chain/` 下 `api_error`、`rate_limit`。这些用例改由 `tests/test_adapter_t05.py` 中
  `test_root_drive_semantics_changed_by_issue_114` 与 `test_root_repair_page_status_is_classified` 的字面断言
  覆盖；其余 T05 文件保留原字节，经上文 #114 偏离变换后比较。
