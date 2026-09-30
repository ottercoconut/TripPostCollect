# 可复用测试运行

使用 [tests/run_lanes.py](../tests/run_lanes.py)，源码副本与输出都必须位于专用临时目录且互不包含。
默认 lane 为 component。不要在生产 checkout 运行完整 pytest；普通 pytest 命令不提供这里的隔离。
测试入口只调用现成 pytest marker/plugin，不更改生产枚举器、调度器或平台入口。

## 准备源码和依赖

使用仅含源码的临时副本，包含 src、scripts、tests、config、db、docs、pyproject.toml、
uv.lock，以及固定版本的 tools/MediaCrawler 源码。不能复制原 data、outputs、temp、
浏览器状态、.env、凭证、.git、旧虚拟环境或历史审计附件。data/outputs 起初必须为空。
当前未提交的新测试和支持文件也必须复制；不能只 git archive HEAD 而漏掉本轮修改。
每个副本按自己的冻结登记核验哈希并恢复 uchg；绝不改变原件标志。
CI 保留不运行测试的 pristine 模板，每个 lane 新建独立源码副本，绝不清理或复用前一轮 data。
根与 fork 的独立锁定环境放在模板外；每个子进程的 PYTHONPATH 绑定自己的源码副本。

根项目 uv.lock 已核实包含 trippostcollect 的 dev extra：pytest、pytest-asyncio、
pytest-cov、ruff、mypy、pre-commit。完整安装用根锁文件，分别在 macOS Python 3.11、3.12
独立副本中准备环境，例如先切换到对应临时源码目录再执行：

```bash
uv sync \
  --locked \
  --extra dev \
  --python 3.11
source .venv/bin/activate
```

另一份副本使用 3.12。依赖准备阶段允许联网，测试阶段禁止；不运行浏览器安装或平台登录。
根入口需要 scrapling[fetchers]。正式 worker 使用根解释器，以
`sys.executable -P -m trippostcollect.platforms.entry` 启动，cwd 为项目根；
过渡装载模块显式提供尚未迁出的 fork 与 scripts 路径，不依赖 cwd 或注入 PYTHONPATH。
fork 独立环境使用子模块锁文件，仅用于 CI 的 fork 离线测试；本入口只跑根 tests/。
fork 测试不能替代根安装验收。

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
使用 GitHub-hosted `macos-26`，Python 3.11/3.12 独立矩阵、`contents: read`，不注入 secrets，
checkout 不保留认证信息。依赖准备阶段联网；根环境使用 `uv sync --locked --extra dev`，
所选 submodule 提交用自己的锁文件创建独立 fork 测试环境。所有执行使用临时源码副本，
没有私人源码、账号数据库或浏览器 profile。完整安装 lane 检查根 CLI `--help`，
fork 离线兼容检查静态导入 B站、微博、抖音、知乎、小红书模块，不调用抓取入口。
fork 测试的 PYTHONPATH 包含根 src/scripts 与 fork 自身路径。
随后在独立 fork 环境执行 `run_matrix.FORK_OFFLINE_TESTS` 明确列出的 19 文件、284 个
原离线用例（小红书与共享辅助测试）；数量变化、skip、xfail 或失败均不可验收。
原浏览器与 CDP 生命周期的 41 个用例已迁入根 `tests/runtime/`，归入 component lane；
知乎的 3 文件、27 个用例已迁入根 `tests/platforms/zhihu/`，同样归入 component lane。
进程、信号和 socket 调用均使用替身，不启动真实浏览器或进程。
微博的 4 个测试文件、24 个用例已迁入根 `tests/platforms/weibo/`，归入 component lane。
抖音的 4 个测试文件、41 个用例已迁入根 `tests/platforms/douyin/`，归入 component lane。
复用现有 pytest、计数插件与执行守卫，使用同一 Seatbelt 无网络/无浏览器策略和临时产物目录。
计数/守卫模块复制为独立名称，根 tests 不进入 PYTHONPATH；显式指定 fork 的 pyproject.toml、
rootdir 和 confcutdir，避免根 conftest、support 或 pytest 配置污染。
CI 显式 setup-node；执行 PATH 保留检测到的 Node 目录、/usr/local/bin 与 /opt/homebrew/bin。
B站正式 article 的行为仍由根项目测试覆盖，上游 video 主循环不作为替代。

[scripts/ci/run_matrix.py](../scripts/ci/run_matrix.py) 无论前一 lane 成败都运行全部四个 lane，
任何失败、缺结果、静态导入失败或 fork 离线测试未全过均使 CI 非零。
执行期 root 三个 lane 与 worker 导入/离线测试使用 Seatbelt。
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
工作流只上传计数、清除 traceback/捕获内容的 JUnit XML、控制证据与 matrix.json，
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
| `tests/test_run_ids.py::test_all_script_run_ids_include_microseconds` | installation | 1 |

issue1_component / issue1_os 是原失败节点的精确标签，不是整个 lane 的别名。
另加 test_guard_cleanup_requires_observed_absence 的 5 个场景：absent、alive、unknown、
scan_error、exits。它们保留真实 Guard、文件、SQLite，所有假 PID 的信号都被拒绝或记录，
通过可控时钟验证持续存活不释放、未知不释放、扫描报错不释放，以及再次观测退出后才释放。
共享 FakeInspector 与 FakeMonotonicClock 从原租约测试提取，没有全局空扫描。

额外发现的真实身份、跨进程 flock、watchdog 子进程、batch checkpoint 强杀测试均标记
macos_process；其中真实自发信号也移到 driver。它们计入完整 OS lane，但不改变原 11 节点计数。
通过 `pytest --collect-only -m macos_process` 可查看当前完整清单，报告必须在仓库外。
组件新增宿主调用时应先明确其能力归属，不能自动注入空扫描或将其悄悄跳过。

## 迁移台账核对

pytest 只校验已提交的迁移台账产物、由符号 JSON 渲染的 C8 附录，以及当前工作树的输入漂移
和迁移进度；纯源码副本不需要 `.git`，也不调用 Git 重建基线。

从基线提交逐字节重建符号与输入台账，必须在根 Git 历史完整、且子模块对象库包含
`docs/adapter-ledger/baseline.json` 登记的 `fork_head` 的 checkout 中执行：

```bash
source .venv/bin/activate
python scripts/dev/adapter_ledger.py symbols --check
python scripts/dev/adapter_ledger.py inputs --check
python scripts/dev/adapter_ledger.py drift
python scripts/dev/adapter_ledger.py progress
```

CI 已在“准备纯源码模板与独立锁定环境”之后加入“核对迁移台账”步骤，使用完整历史的
checkout 和已创建的 root-venv 执行上述四项核对；独立测试 lane 继续在无 Git 的源码副本中运行。
缺失基线对象时工具报告错误，不自动拉取历史，也不回退为工作树重建。
