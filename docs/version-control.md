# 本地分支与版本管理

## 科研证据与主分支

版本管理服务于科研采集证据的真实性、完整性与可追溯性。评审改动时，先说明它解决哪一种证据
问题：例如中断丢失已完成批次、把验证或网络失败误判为完成、重复启动同一任务，或无法还原采集
配置。可靠性和必要的维护改动可以保留；没有明确用途的临时工具先归档，不因“已有代码”直接合并。

根仓库以 `main` 作为唯一长期本地分支。任务分支只在开发和验证期间存在，结束时把有效改动合入
`main`（须有对应合并授权），核对已替代的实现并归档历史，再删除任务分支。未获授权时保留任务分支。
`main` 不靠合并全部旧分支来凑齐历史；
cherry-pick、rebase 和后续修复已经吸收的改动，以最终实现及其验证证据为准。

采集工具的版本整理不应顺带改写历史数据、执行状态、日志或数据备份。确有独立的数据删除需求时，
应另外明确授权范围和证据处置方式；不要把批量销毁历史副本的工具当作日常整理步骤。

## 仓库与历史 fork

根仓库远程是 `origin`（`ottercoconut/TripPostCollect`，私有）。T14（#19）起根仓库不再包含
`tools/MediaCrawler/` 子模块：所需 MediaCrawler 派生代码已收编到 `src/trippostcollect/`，保留原版权头，
原许可随包资源 `resources/licenses/MediaCrawler-LICENSE` 发布。个人改造备份 `ottercoconut/MediaCrawler`
只作历史存档，不再随根仓库更新或配对；T14 之前的根提交仍钉住其 gitlink，检出这些历史提交时需另行
取得对应子模块提交。纯本地参照 `../MediaCrawler-upstream` 是上游只读 clone，仅供对照，不进入任何仓库。

## 开始与提交

1. 检查 `git status --short`、`git branch -vv` 和 `git worktree list`。
   发现已有未提交改动时，先确认归属；不把它们自动暂存、提交、丢弃或带进无关合并。
2. 新任务通常从已验证的 `main` 创建任务分支，按主题使用 `docs/...`、`test/...`、`chore/...`、
   `refactor/...` 或 `fix/...`，不要求工具名称前缀。工作区有其他任务的改动且需要
   隔离时使用独立 worktree；不为切换分支执行 `reset --hard` 或 `clean`。
3. 一个提交只处理一个问题，按文件或补丁暂存，并用 `git diff --cached` 检查内容。标题说明
   实际行为变化，可使用现有的 `fix(...)`、`feat(...)`、`docs(...)`、`chore(...)` 风格。
   commit、issue、PR 的标题和正文使用中文，技术标识符可保留原样；一提交一主题。
   review 由助手检查实际 diff 与测试证据，列明失败、未执行项和剩余缺口，不让用户代做技术评审。
4. 按 [AGENTS.md](../AGENTS.md) 执行对应验证。提交前始终激活根项目虚拟环境并验证冻结资产：

   ```bash
   source .venv/bin/activate
   python scripts/verify_frozen_files.py
   git diff --cached --check
   ```

5. 提交完成后检查暂存区和剩余改动。SQLite、抓取产物、浏览器登录态、环境秘密和其他已忽略的
   运行数据不进入 Git；不要用 `git add -f` 绕过现有忽略规则。

`AGENTS.md` 的治理限制和 `config/frozen_files.json` 的冻结规则继续适用；分支整理不构成
解冻授权。包版本号只随明确的发布变更更新，清理分支不增加版本号。
文档或保护性测试完成也不构成采集实现、真实采集、治理解冻、合并或推送授权。

平台适配 P00 在 `chore/platform-adapter-preflight` 上完成，经用户授权推送并以 PR 合入 `main`；
实施任务卡以 GitHub issues（label `platform-adapter`）跟踪，每张实施卡使用独立任务分支和 PR。
文档与 issue 就绪不触发实现或采集，各卡另需实现授权。
准备与验证证据见 [P00 工作单](platform-adapter-preflight.md)。

## 合并与版本

- `main` 保存已验证的集成结果。待合并分支必须与对应 `main` 比较，检查独有提交和最终文件树
  差异，不能仅凭分支名称或提交日期判断已完成。
- 工作区准备好、验证通过后再更新 `main`。快进使用 `git merge --ff-only <分支名>`；出现分叉时先在
  集成分支处理并重新验证，不自动创建未经检查的合并。
- 本地设置 `pull.ff=only`，使 `git pull` 遇到分叉时停止；`push.default=simple`
  使用同名分支推送规则。这些设置位于 `.git/config`，不会随 clone 复制，且不代替人工核对。
- 拉取或快进会改动冻结资产（`config/frozen_files.json` 登记且本机带不可变标志的文件）时，`git pull`
  无法替换这些文件，快进会中途停下、工作树只更新一部分。先用
  `git diff --name-only HEAD..origin/main` 确认要变动的冻结文件，再按以下顺序执行（T14-C 的
  具体文件与迁移前提见 [运行手册](operations-runbook.md)「拉取 T14 删除批（T14-C）」）：
  1. 确认没有 runner、worker 或 warmup 在运行；拉取 T14-C 前还须已在 T14-A 版本上按运行手册完成并核对
     非小红书登录资料迁移，并建议把 `tools/MediaCrawler/browser_data` 另行备份到仓库外。
  2. 在当前 `main` 上运行 `python scripts/verify_frozen_files.py`，必须通过。
  3. 只对将被改动的冻结文件解除标志（T14-C 为 `docs/crawl-architecture.md` 与
     `docs/data-persistence.md`）。macOS 与 Linux 分别为：

     ```bash
     chflags nouchg \
       docs/crawl-architecture.md \
       docs/data-persistence.md
     sudo chattr -i \
       docs/crawl-architecture.md \
       docs/data-persistence.md
     ```

  4. 确认 `git config --get submodule.recurse` 无输出，再执行 `git pull --ff-only`；不带
     `--recurse-submodules`。拉取失败时先看 `git status --short`，不用 `reset --hard` 或 `clean` 处理。
  5. 恢复标志（macOS `chflags uchg`，Linux `sudo chattr +i`，对象同第 3 步），再运行
     `python scripts/verify_frozen_files.py`，必须通过。
  6. 不要运行 `git submodule deinit tools/MediaCrawler`：它会删除旧子模块工作树及其中的
     `browser_data` 备份；拉取后残留的 `tools/MediaCrawler/` 已被 `.gitignore` 忽略。
- 需要保留发布或回滚版本时，创建带说明标签，记录完整 SHA 和验证结果。
  已使用的标签不移动；后续修复使用新标签。标签只覆盖已提交文件，不包含未提交改动和运行数据。

## 清理分支

只把已完整合入本仓库 `main`、未被任何 worktree 占用的任务分支列为直接删除候选。
当前分支、`main`、明确保留的长期分支和 `safety/*` 备份分支不自动清理。

清理前保存引用清单和完整 bundle，并验证 bundle：

```bash
branch_backup_dir="$(git rev-parse --absolute-git-dir)/branch-cleanup/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$branch_backup_dir"
git for-each-ref \
  --format='%(refname) %(objectname)' \
  refs/heads \
  > "$branch_backup_dir/branches-before.txt"
git bundle create \
  "$branch_backup_dir/before-cleanup.bundle" \
  --all
git bundle verify \
  "$branch_backup_dir/before-cleanup.bundle"
git branch \
  --merged main
```

逐一确认候选的完整提交 SHA、祖先关系与 worktree 占用情况后，使用 `git branch -d <分支名>`。
`-d` 也可能按该分支的 upstream 判断可删，不能代替前面的 `main` 祖先检查。删除拒绝时重新核对，
不自动升级为 `-D`。完成后检查剩余引用，并确认当前分支、`main`、暂存区与原有工作区内容未变化。

`git cherry main <分支名>` 可帮助识别 cherry-pick 或 rebase 后的补丁等价提交，但不能证明完整
历史已合并、后续没有撤销或合并提交没有独有内容。这类分支先保留，待核对最终代码并确定归档
方式后再处理。有独有补丁的分支还需用 `git range-diff` 比较整合版本，并检查最终行为和相关测试。

当用户明确要求收敛到单一 `main` 时，应逐分支记录原 SHA、科研用途、对应的整合提交及取舍理由。
确认有效行为已保留、完整历史已经归档且 bundle 可恢复后，才可使用 `git branch -D <分支名>`
移除不属于 `main` 祖先的旧引用。未提交改动另外保存补丁与文件原件；bundle 无法替代这份备份。
结束时验证 `refs/heads` 仅有 `main`、当前检出为 `main`，并检查工作区和冻结资产。

## 恢复与备份边界

本次整理的备份位于 Git 目录的 `branch-cleanup/<时间>/`，其中 `audit.json` 记录完整分支
SHA、删除清单、工作区校验结果和本地配置变更；`before-cleanup.bundle` 保存清理前的 Git 历史。
后续手工清理可使用上面的 `branches-before.txt` 作为引用清单。恢复时，先从对应清单取得完整
提交 SHA，并确认目标分支名尚不存在：

```bash
git bundle verify \
  /绝对路径/before-cleanup.bundle
git bundle unbundle \
  /绝对路径/before-cleanup.bundle
git branch \
  恢复的分支名 \
  清理前的完整提交SHA
```

`unbundle` 导入 Git 对象，不检出文件；随后创建分支引用也不会覆盖当前工作区。恢复后检查新
分支的 SHA 与记录一致。

bundle 不包含未提交或未跟踪文件、Git 配置、hooks、SQLite、图片和登录态。同一磁盘内的
`.git/branch-cleanup/` 只用于撤销清理；它不是异地备份。需要机器故障恢复时，应另行保存
仓库的 Git 备份和必要的业务数据，并按各自数据与秘密管理要求处理。
