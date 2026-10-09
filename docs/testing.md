# 可复用测试运行

使用 [tests/run_lanes.py](../tests/run_lanes.py)，源码副本与输出都必须位于专用临时目录且互不包含。
默认 lane 为 component。不要在生产 checkout 运行完整 pytest；普通 pytest 命令不提供这里的隔离。
测试入口只调用现成 pytest marker/plugin，不更改生产枚举器、调度器或平台入口。

## 准备源码和依赖

使用仅含源码的临时副本，包含 src、scripts、tests、config、db、docs、pyproject.toml、
uv.lock、build_support.py、MANIFEST.in、`.github/workflows/macos-test-lanes.yml`（工作流配置本身受测）。
T14 起仓库不再含 fork 子模块。不能复制原 data、outputs、temp、
浏览器状态、.env、凭证、.git、旧虚拟环境或历史审计附件。data/outputs 起初必须为空。
当前未提交的新测试和支持文件也必须复制；不能只 git archive HEAD 而漏掉本轮修改。
每个副本按自己的冻结登记核验哈希并恢复不可变标志（macOS `uchg`、Linux `chattr +i`，实现见
[scripts/ci/frozen_flags.py](../scripts/ci/frozen_flags.py)）；绝不改变原件标志。
CI 保留不运行测试的 pristine 模板，每个 lane 新建独立源码副本，绝不清理或复用前一轮 data。
根锁定环境放在模板外；每个子进程的 PYTHONPATH 绑定自己的源码副本。

根项目 uv.lock 已核实包含 trippostcollect 的 dev extra：pytest、pytest-asyncio、
pytest-cov、ruff、mypy、pre-commit，以及与 `[build-system]` 同一约束的 setuptools（供测试期离线构建）。项目只支持 Python 3.12；完整安装用根锁文件，
在 macOS Python 3.12 独立副本中准备环境，例如先切换到对应临时源码目录再执行：

```bash
uv sync \
  --locked \
  --extra dev \
  --python 3.12
source .venv/bin/activate
```

依赖准备阶段允许联网，测试阶段禁止；不运行浏览器安装或平台登录。
根入口需要 scrapling[fetchers]。正式 worker 使用根解释器，以
`sys.executable -P -m trippostcollect.platforms.entry` 启动，cwd 为项目根；
新入口与五站装配只用根包：配置对象由 `worker_inputs.worker_config()` 提供，不把 scripts 插入 `sys.path`。
原过渡装载模块 `platforms/_fork_bridge.py`、私有桥与 fork 独立环境已在 T14 删除；本入口只跑根 tests/。

## 构建与仓库外安装

包内资源分两类：JS 与 MediaCrawler LICENSE 位于 `src/trippostcollect/resources/`；SQL（`db/*.sql`）与
`docs/formal-crawl-contract.md` 以仓库现址为唯一编辑真源，白名单只在 `core/resources.py:GENERATED_RESOURCES`
定义。构建时 `build_support.BuildPy`（pyproject `cmdclass` 登记）按该白名单复制进 wheel 的
`trippostcollect/resources/sql/`、`resources/contracts/` 并校验 SHA-256；`MANIFEST.in` 让 sdist 携带真源与构建模块。
源码树不保存生成副本；源码 checkout（含可编辑安装）直接读真源。建库 SQL 经资源入口读取；契约文档的
路径与散列记录仍指仓库 `docs/`。

安装包不在源码 checkout 内运行时，必须设置现有 `TRIPPOST_PROJECT_ROOT` 指向工作根，否则导入
`trippostcollect.core.paths` 明确失败；源码 checkout 未设置时仍以仓库为根。
installation lane 的 `tests/test_adapter_t12_install.py` 在副本中用锁定环境的 setuptools 离线构建 sdist 与 wheel
（并由 sdist 再构建 wheel），在仓库外全新 venv 中解包安装，核对资源字节、全部子模块导入、各正式入口 `--help`
与 main 逐字相同，以及仓库外工作根中的四站选站装配。

## 运行与结果

TPC_TEST_SOURCE 指向准备好的专用临时源码副本。每轮 -o 必须是新的仓库外短路径
（socket 路径受系统长度限制），例如：

```bash
source .venv/bin/activate
python tests/run_lanes.py \
  --source "$TPC_TEST_SOURCE" \
  --lane component \
  -o /private/tmp/tpc-component-311
```

必须从外层真实仓库启动上述 runner；另两次将 lane 改为 socket、installation，
并使用各自全新的源码副本和输出目录。不能从测试副本自身启动 runner。
输出为 sandbox.sb、pytest.log、pytest.xml、counts.json、network-probes.json、result.json；
缺能力或依赖返回非零。counts 区分 collected、selected、deselected、passed、failed、errors、
skipped、xfailed、xpassed。空收集、skip、xfail、xpass 不可作为通过；
result.json 将其他 lane 标记为“未运行”，未产生计数时为 null，不能算作零失败通过。
父级预检失败可能尚无 XML，命令非零本身就是不可验收，不能仅靠 XML 是否存在判断成功。

| lane | 选择表达式 | 执行边界 |
|---|---|---|
| component | not macos_process and not local_socket and not installation | macOS Seatbelt allow-default，保留动态库/Python 运行时能力；显式禁止凭证/生产数据访问、临时目录外写入、IP 网络、AF_UNIX bind、浏览器执行 |
| socket | local_socket | 同上；仅增加本轮输出 t/ 下 AF_UNIX bind；先检查目录外 bind、TCP 被拒绝 |
| installation | installation | 同组件隔离；真实导入根依赖与 CLI，保留 run-ID 格式断言 |
| os | macos_process | 独立 GitHub 托管 macOS VM，外部网络/浏览器策略；真实枚举、身份、组与 profile 预检 |

asyncio 插件在禁用自动加载后显式加载。进程信号用例通过 support/signal_driver.py 在独立
pytest driver 执行原函数全部断言，父进程检查退出码和临时 XML；不再向主 pytest 发真实信号。
主 pytest 与 signal driver 都显式加载独立 execution_guard 插件；driver 使用独立计数与守卫报告，
不会覆盖父进程 counts。subprocess/exec 的 canary 使用不存在的浏览器/open/osascript 路径，
只有守卫实际拒绝才通过，不启动真实浏览器。

## 一次性托管 macOS CI

当前个人账号不能解除 sandbox 跑 OS lane。macOS sandbox 内 ps 不可用时直接视为缺能力，
不能通过给 ps 添加 allow 规则、替换系统程序、sudo 或 broker 绕过。
Linux 通过不能替代 macOS 证据。

工作流 [.github/workflows/macos-test-lanes.yml](../.github/workflows/macos-test-lanes.yml)
使用 GitHub-hosted `macos-26`，Python 3.12 单一版本、`contents: read`，不注入 secrets，
checkout 不保留认证信息。依赖准备阶段联网；根环境使用 `uv sync --locked --extra dev`，
这是唯一测试环境。所有执行使用临时源码副本，
没有私人源码、账号数据库或浏览器 profile。完整安装 lane 检查根 CLI `--help`，并构建 wheel/sdist 做仓库外安装验收。
根环境选站装配检查（matrix 的 `assembly`）在独立副本中导入 B站正式 article 模块
（`run_matrix.ROOT_ASSEMBLY_MODULES`）并对四站调用 `trippostcollect.platforms.entry.load_crawler`，
不调用抓取入口，也不得装载原 fork 路径下的任何包；上游 B站视频主循环不再导入。
原 fork 离线用例已全部移植到根 tests：T14-B2 把最后 4 文件、34 个用例按台账 target_file 原名原断言移植到
`tests/artifacts/test_staging.py` 与 `tests/application/test_discovery.py`（component lane，映射见
`tests/fixtures/t14_fork_test_mapping.json`）；T14-C 删除 fork 子模块后没有 gitlink，fork 离线 lane 不再运行，CI 也不再
准备 fork 环境。
原浏览器与 CDP 生命周期的 41 个用例已迁入根 `tests/runtime/`，归入 component lane；
知乎的 3 文件、27 个用例已迁入根 `tests/platforms/zhihu/`，同样归入 component lane。
进程、信号和 socket 调用均使用替身，不启动真实浏览器或进程。
微博的 4 个测试文件、24 个用例已迁入根 `tests/platforms/weibo/`，归入 component lane。
抖音的 4 个测试文件、41 个用例已迁入根 `tests/platforms/douyin/`，归入 component lane。
小红书的 15 个测试文件已迁入根 `tests/platforms/xhs/`，共 249 个用例，归入 component lane。
CI 显式 setup-node；执行 PATH 保留检测到的 Node 目录、/usr/local/bin 与 /opt/homebrew/bin。
B站正式 article 的行为仍由根项目测试覆盖，上游 video 主循环不作为替代。

[scripts/ci/run_matrix.py](../scripts/ci/run_matrix.py) 无论前一 lane 成败都运行全部四个 lane，
任何失败、缺结果或选站装配失败均使 CI 非零。
执行期 root 三个 lane 与选站装配使用 Seatbelt。
OS lane 调用 [scripts/ci/native_macos.py](../scripts/ci/native_macos.py)：每次管理员操作均检查
GitHub-hosted/macOS/镜像环境标志；优先向已有 Apple wildcard anchor 添加临时 PF 子规则。
没有 dispatcher 时，只有确认根过滤/NAT规则、anchors、tables、states 全空才加载最小临时
dispatcher，finally 恢复空规则；未知已占用策略拒绝替换。缺控制能力即失败。

PF 部署前分别建立 IPv4/IPv6 回环监听者并验证连接成功，部署后验证这些仍存活的监听者
连接超时，并要求 PF 规则有实际包计数；执行后再次验证。原生探针不要求 PF 阻断 AF_UNIX。
Seatbelt 探针独立要求 connect/bind 的 EPERM/EACCES，socket 构造失败不能冒充连接被拒绝。
禁止把两种控制层的超时、拒绝错误混用。

浏览器执行采用已审阅测试专用的 Python 防误用守卫，结合一次性无私人数据 VM 与 PF 网络限制；
这不是可承载任意恶意代码的安全沙箱。守卫覆盖 Python subprocess/exec/posix_spawn/system 边界，
不修改系统浏览器、权限或 SIP。ExitStack/finally 清空仅本轮 PF anchor，恢复本轮新增 dispatcher，
并释放本轮 PF enable token；恢复后重新验证监听者可达、原规则和启用状态。
SIGTERM 进入同一清理路径。不会在本机部署此策略。OS lane 不接受外部 `enforced=true` 回执。

```bash
source .venv/bin/activate
python tests/run_lanes.py \
  --source "$TPC_TEST_SOURCE" \
  --lane os \
  -o /private/tmp/tpc-os-311
```

上面的 OS 命令只允许在托管 VM 内使用。`os-policy.json` 记录真实控制层、探针、PF 计数及恢复状态。
工作流只上传计数、清除 traceback/捕获内容的 JUnit XML、控制证据、matrix.json 与 coverage.json，
不上传源码、原始日志、SQLite 或浏览器存储。
counts.json 的 failure_diagnostics 仅保留失败节点（去掉参数值）、源码相对文件/行号、
阶段与白名单错误类型，不导出异常文本、rawrepr、headers 或局部变量。
真实 watchdog 暂停恢复集成测试使用 1 秒 inactivity 预算、至少 3 秒暂停和独立 ready/recovered
握手，验证暂停不消耗预算，不承诺 100ms 调度性能；纯时钟测试继续验证精确扣时和恢复边界。
watchdog 的真实 terminal 集成先等待 child ready，再观测 terminal；正常 unwind 在父进程
消费首次事件后才 release，保留退出码 7、非超时及固定原因。重复 terminal 使用 2 秒固定
grace，child 等待未释放的握手，必须由父进程以 124 停止。父侧握手观测有 15 秒外层上限，
child 自身有 20 秒等待上限；启动迟到不能无限伪装为 network_paused。这些上限是失败边界，
不是自动重试。重复事件不得滑动 deadline 的精确边界由同文件假进程与可控时钟验证；假 PID
的 kill/killpg 被拦截，Popen 被禁止，该用例属于 component。
持续进度测试保留实际文件更新及运行时间超过 inactivity 预算的关系断言，使用 2 秒预算与
10 秒首次进度宽限；输出清理测试在安装 SIGTERM 处理器并输出后写 ready，首次进度结束
启动宽限，之后无进度必须停止且输出各一次。finalizing 测试也用 ready/release 代替 sleep。
注册失败、gate 取消与信号注入仍验证同步事件次序及资源回收；身份变化测试的耗时上限仅作
15 秒外层保护，不要求共享 VM 在 2 秒内完成清理。真实信号及这些 native 用例只在 Actions
OS lane 验收，本地纯时钟通过不能替代两版本真实 OS 结果。
通用 runner 中断用例（`tests/test_crawl_runner_interrupt.py`）用真实进程贯通 runner → 中间层 →
独立 worker 进程组，只覆盖 `crawl_runner.py` 通用路径，不覆盖小红书租约链路。runner 驱动经
`run_cli` 运行，只把生产 child 命令的脚本换成 `tests/support/runner_interrupt_fakes.py`，其余参数照用；
中间层走真实入口包装 `run_main_with_operator_interrupt`、`collection.main`、生产 `parse_args` 与
`run_command`，worker 走真实 `runtime.worker.run`，用生产 `AdaptiveAccumulator` 与 worker 事件出口写出
一批已确认批次和一个未确认尾批；worker 正常结束时中间层用生产 `load_pagination_evidence` 与
`persist_discovery_checkpoint` 提交，runner 用生产逻辑更新 campaign。平台抓取、正文导入与图片物化
不在本组证明范围内；B站进程内浏览器、以 `setsid` 启动的 CDP Chrome、对整个 cgroup 同时发信号的
场景以及小红书租约路径（#58）也不在覆盖内。各层写 ready/身份文件握手，测试只轮询这些文件和临时 SQLite，不用固定 sleep；
断言 SIGINT/SIGTERM/SIGHUP 退出码、单次温和信号与清理标记、两层 stdout/stderr 各一次、进程组消失、
未中断对照会推进而中断不推进 checkpoint/seen/campaign、提交后中断保留已提交前沿但不更新 campaign、
两条通道同时活跃时都被收束、已成功通道保留为成功、排队 job 不派发、强杀兜底同时收束已登记的
worker 组并保留首因、child 退出晚于锁存被观察到时按中断处理，以及 Popen 期间与收尾阶段的信号。
提交工作流不等于 CI 验收通过；必须由 Actions 的两版本实际产物证明。不要在私人宿主伪造环境变量。

## issue #1 节点映射

节点名称保持不变，仅增加 marker；参数化计数如下。component 合计 17，os 合计 11。

| 原节点（仍是现节点） | lane | 数量 |
|---|---|---:|
| `tests/test_xhs_leases.py::test_normal_end_releases_exact_lease` | os | 1 |
| `tests/test_xhs_leases.py::test_guard_removes_runtime_session_before_releasing_database_lease` | component | 1 |
| `tests/test_xhs_leases.py::test_ordinary_exception_releases_without_changing_health` | component | 1 |
| `tests/test_xhs_leases.py::test_runtime_session_cleanup_failure_retains_exact_lease` | component | 1 |
| `tests/test_xhs_leases.py::test_incomplete_runtime_session_removal_retains_exact_lease` | component | 2 |
| `tests/test_xhs_leases.py::test_guard_does_not_delete_session_it_failed_to_create` | component | 1 |
| `tests/test_xhs_leases.py::test_guard_owns_and_removes_session_after_mid_creation_failure` | component | 1 |
| `tests/test_xhs_leases.py::test_guard_retains_claimed_session_when_marker_no_longer_matches` | component | 2 |
| `tests/test_xhs_leases.py::test_guard_does_not_follow_preexisting_session_root_symlink` | component | 1 |
| `tests/test_xhs_leases.py::test_child_registration_failure_stops_untracked_process_group` | os | 1 |
| `tests/test_xhs_leases.py::test_signal_during_child_registration_cancels_gate_before_target_exec` | os | 2 |
| `tests/test_xhs_leases.py::test_keyboard_interrupt_during_child_registration_never_execs_target` | os | 1 |
| `tests/test_xhs_leases.py::test_child_gate_release_failure_never_execs_target_or_leaves_process` | os | 1 |
| `tests/test_xhs_leases.py::test_gated_child_keeps_registered_pid_and_process_group_after_exec` | os | 1 |
| `tests/test_xhs_leases.py::test_sigterm_stops_process_group_before_release` | os | 1 |
| `tests/test_xhs_leases.py::test_sigkill_keeps_lease_and_live_child_blocks_orphan_recovery` | os | 1 |
| `tests/test_xhs_leases.py::test_recovery_cli_uses_lease_id_and_accepts_missing_terminal_state` | component | 1 |
| `tests/test_xhs_pool.py::test_xhs_operator_interrupt_finalizes_state_summary_and_exact_cleanup` | component | 2 |
| `tests/test_xhs_pool.py::test_xhs_schema_removes_persistent_fields_without_erasing_history_or_files` | component | 1 |
| `tests/test_xhs_post_repair.py::test_xhs_repair_interrupt_writes_terminal_audit_before_exact_cleanup` | component | 2 |
| `tests/test_xhs_terminalizer.py::test_formal_os_signal_immediately_after_acquire_has_one_failed_terminal_commit` | os | 2 |
| `tests/test_xhs_terminalizer.py::test_sms_terminal_before_pagination_keeps_precise_reason_and_checkpoint` | component | 1 |
| `tests/test_xhs_runtime_status.py::test_socket_is_rejected_without_blocking` | socket | 1 |
| `tests/test_platform_package_resources.py::test_all_script_run_ids_include_microseconds`（原在 `tests/test_run_ids.py`，T12 按台账迁移，名称不变） | installation | 1 |

issue1_component / issue1_os 是原失败节点的精确标签，不是整个 lane 的别名。
另加 test_guard_cleanup_requires_observed_absence 的 5 个场景：absent、alive、unknown、
scan_error、exits。它们保留真实 Guard、文件、SQLite，所有假 PID 的信号都被拒绝或记录，
通过可控时钟验证持续存活不释放、未知不释放、扫描报错不释放，以及再次观测退出后才释放。
共享 FakeInspector 与 FakeMonotonicClock 从原租约测试提取，没有全局空扫描。

额外发现的真实身份、跨进程 flock、watchdog 子进程、batch checkpoint 强杀测试均标记
macos_process；其中真实自发信号也移到 driver。它们计入完整 OS lane，但不改变原 11 节点计数。
通过 `pytest --collect-only -m macos_process` 可查看当前完整清单，报告必须在仓库外。
组件新增宿主调用时应先明确其能力归属，不能自动注入空扫描或将其悄悄跳过。

## 五站覆盖守护（T13）

[tests/fixtures/t13_coverage.json](../tests/fixtures/t13_coverage.json) 是 F01–F15 × 五站（B站、微博、抖音、知乎、小红书）
的机器可读覆盖声明：每格为 `covered`（列出本站 node）、`shared`（平台无关的共享实现，写明依据）、
`na`（规格限定他站，写明依据），不接受其他状态。条目可写精确 `node`、`function`（可带必须出现的 `params` 与本站证据例外
`site_basis`）或 `file`（`min_count`/`count`/`min_functions`）；原 fork 离线条目已随移植改为 component lane 的 `min_functions` 条目。

- [tests/test_t13_coverage.py](../tests/test_t13_coverage.py)（component）按 `run_lanes.EXPRESSIONS` 的四个表达式各做一次
  `--collect-only`，经 `tests/support/coverage_probe.py` 记录标记，断言每个声明 node 只落在声明的 lane、
  不带 skip/xfail、skipif 条件全为假，并核对每格条目确有本站证据（站点目录、文件或函数名、参数 ID、
  函数体字面量，或单站卡对照文件）。运行期 `pytest.skip()` 由 lane 计数校验（skipped=0）兜底。
- 同一用例精确断言 issue #1 的 `issue1_component` 17 项、`issue1_os` 11 项、socket 与 installation 各 1 项，
  以及 issue #2 的 `tests/test_xhs_lease_exit.py` 68 项。
- `legacy_equivalence: true` 标记依赖冻结旧实现 fixture 的 T05–T10 对照测试。T14 删除这些测试时守护会
  因缺失而失败，必须先补上不依赖旧实现的覆盖再改声明，这是预期行为。
- CI 中 `run_matrix.py` 在全部 lane 后调用
  [scripts/ci/coverage_report.py](../scripts/ci/coverage_report.py)，按声明读取各 lane 的 junit，
  写出 `coverage.json`（F×站逐格匹配与通过数、按站×lane 的声明/通过计数、各 lane 总计、每站状态计数）；任一声明 node 缺失、未通过或某 lane 无 junit 都使 CI 非零。报告只含 node 名与计数。

新增或删除承担五站责任的测试时同批更新声明；不得为了让守护变绿而删除声明或改用他站用例。

## 迁移台账核对

pytest 只校验已提交的迁移台账产物、由符号 JSON 渲染的 C8 附录，以及当前工作树的输入漂移
和迁移进度；纯源码副本不需要 `.git`，也不调用 Git 重建基线。

T14 删除 fork 子模块后台账冻结：`symbols`、`inputs`、`baseline` 不带 `--check` 的生成
以及 `tests` 收集均拒绝运行，不再从基线提交重建；`--check` 只做不访问 Git 对象库的冻结自检（登记散列、C8 附录由 JSON 逐字节重现、
规则与冻结行一致），不需要子模块对象库。在仓库 checkout 中执行：

```bash
source .venv/bin/activate
python scripts/dev/adapter_ledger.py symbols --check
python scripts/dev/adapter_ledger.py inputs --check
python scripts/dev/adapter_ledger.py drift
python scripts/dev/adapter_ledger.py progress
```

CI 在“准备纯源码模板与独立锁定环境”之后的“核对迁移台账”步骤用已创建的 root-venv 执行上述四项核对；
独立测试 lane 继续在无 Git 的源码副本中运行。

## T14-C 删除批的测试偏离登记

fork、私有桥与过渡模块删除后，依赖它们的对照用例按以下登记处理；固化文件与冻结 fixture 均不改字节。

- [tests/support/fork_removal_deviation.py](../tests/support/fork_removal_deviation.py)：T08/T10/T11 冻结旧源码执行前
  先核对原字节哈希，再把已删除的 `MEDIACRAWLER_DIR` 从 `core.paths` 导入中移除并按原值预置，把
  `from execution_state import` 改为 `trippostcollect.core.execution_state` 的同名导入，其余源码逐字执行。
- `tests/support/platform_session_deviation.py` 的 `xhs_cdp_settings_without_user_data_dir`：T09 小红书场景预期中
  每条 `cdp_manager` 记录先钉住 `USER_DATA_DIR == "%s_user_data_dir"`，再删除该键后与根实现比较。
- `tests/test_adapter_t12.py` 的 `T14C_DELETED_LEDGER_NODES` 登记 6 个随删除批消失的台账节点（只测旧导出
  hook 或 fork 评论脱敏的用例）；台账节点对账总数相应扣除。
- 台账收口规则新增 fork `tools/trippostcollect_adaptive.py:env_int` → `env_int_reader`：T03 迁入的单次读取包装
  `worker_inputs.env_int` 只供 fork 旧委托，随删除批删除，根实现统一经零参 `env_int_reader` 读取。
- `TRIPPOSTCOLLECT_XHS_KEEP_AUTHOR_DETAIL` 已登记到 `adapter_ledger.AUTHORIZED_ENV_REMOVALS`，`drift` 不把它计为
  意外删除。

本机沙箱脚本（如 Linux 工作副本的 `tpc_pytest.sh`）直接在新 worktree 上运行时没有可写 `data/`，t02/t11/t12
中约 20 个用例会出现环境性失败；这不是代码回归，正式结论以卡片闸门（临时源码副本）和 CI 为准。

## 本机卡片闸门：macOS 与 Linux 并行

[scripts/dev/card_gate.py](../scripts/dev/card_gate.py) 是开发期复核工具，按运行平台选择沙箱后端：
macOS 为 Seatbelt（[sandbox_macos.py](../scripts/dev/sandbox_macos.py)），Linux 为 bubblewrap
（[sandbox_linux.py](../scripts/dev/sandbox_linux.py)）。两者语义逐项对应，子孙进程继承限制；
闸门编排进程在沙箱外，每个检查子进程单独套沙箱，启动前先跑 canary，任一项未被拒绝即终止。

| 约束 | macOS | Linux |
|---|---|---|
| 禁 IP 网络，保留 AF_UNIX | Seatbelt，connect/bind 得 EPERM | 独立网络命名空间 + seccomp，AF_INET/AF_INET6/AF_PACKET 套接字构造得 EPERM，禁 io_uring |
| 禁浏览器与桌面打开器 | process-exec 路径正则 | 名称匹配同一正则的可执行文件/目录与 xdg-open 等被遮蔽，exec 得 EACCES |
| 只写临时根与 /dev | file-write* require-not | 根只读绑定，写入得 EROFS |
| 禁读写本机浏览器用户数据 | `~/Library/Application Support` 下 Chrome 目录 | `~/.config` 下 Chrome/Chromium/Chrome for Testing 目录被 000 空目录遮蔽 |
| 禁读写项目平台登录资料 | checkout 内 `data/runtime/platform_sessions` 禁读写 | checkout 内现存的 `data/runtime/platform_sessions` 被 000 空目录遮蔽 |
| 冻结副本不可变标志 | `chflags uchg`（所有者可设） | `chattr +i`（需 root，非 root 经 `sudo -n`） |

Linux 前置条件：安装 `bubblewrap`；Ubuntu 23.10 起默认限制非特权用户命名空间，需为
`/usr/bin/bwrap` 放行（AppArmor profile 含 `userns,`）；建立测试副本时设置冻结副本 `chattr +i`
需免密 sudo。执行守卫在 Linux 额外拒绝 xdg-open、sensible-browser、x-www-browser 等打开器。

两平台的失败集合各自与同平台基线比较。Linux 不限制 `ps`，os 组在本机沙箱下可全部运行；
这只是开发期复核，正式 OS 结论与 Seatbelt EPERM 证据仍以托管 macOS CI 为准。

