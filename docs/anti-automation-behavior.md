# 反自动化检测与人类行为模拟方案

本文说明本项目在本次 CTF 授权靶场中做页面抓取、资源取证和后续利用时，如何降低自动化行为特征、引入随机时间间隔，并把人类行为模拟稳定接入现有工具链。所有策略默认只用于仓库配置的沙箱目标、授权页面和本项目产物目录。

## 设计原则

从靶场目标的开发视角看，行为分析通常不会只看单个字段，而会组合请求间隔、会话连续性、浏览器指纹、页面停留、滚动轨迹、资源加载、登录态变化和异常失败率。项目侧应把对抗点放在运行节奏和浏览器行为层，而不是在单次请求里堆叠大量 header。

当前项目已经有三层控制面：

| 层级 | 入口 | 作用 |
|---|---|---|
| 调度层 | `scripts/crawl_runner.py`、`config/crawl_targets.json` | 控制任务周期、尝试次数、是否 headed、是否导入结果。 |
| 站点策略层 | `scripts/web_sites.py`、`scripts/crawl_policy.py` | 控制最小间隔、随机抖动、单会话上限、每日预算、冷却和站点锁。 |
| 浏览器行为层 | `scripts/human_flow.py`、`scripts/ctf_resource_crawl.py` | 控制 viewport、UA、runtime hints、停留、鼠标移动、滚轮、CDP 触摸滚动和评论区概率访问。 |

默认路线是低频、串行、headed、有持久 profile、有证据日志。`--no-throttle` 只用于本地快速验证，不能作为正式利用或持续取证配置。

## 随机时间间隔

站点级请求节奏由 `site_request_guard()` 强制执行。它会读取 `web_sites.py` 中的 `min_delay_seconds`、`jitter_ratio`、`min_jitter_seconds`、`max_jitter_seconds`、`max_requests_per_session`、`daily_request_budget` 和 `cooldown_minutes`，并把状态写入 `data/runtime/scrapling_throttle.json`。

新增站点或调高频率时，先改 `scripts/web_sites.py`，不要在业务脚本中直接 `sleep()`：

```python
WebSite(
    key="example",
    name="Example",
    default_url="https://example.test/page",
    login_url=None,
    cookie_domains=("example.test",),
    login_hosts=(),
    login_required=False,
    recommended_mode="dynamic",
    min_delay_seconds=180,
    jitter_ratio=0.35,
    min_jitter_seconds=8.0,
    max_jitter_seconds=90.0,
    max_requests_per_session=6,
    daily_request_budget=18,
    cooldown_minutes=120,
    account_risk_level="high",
)
```

脚本内需要发起额外请求时，直接包住关键网络动作：

```python
from crawl_policy import site_request_guard, varied_wait_seconds
from web_sites import get_site

site = get_site("zhihu")

with site_request_guard(site, label="ctf-extra-probe:detail") as event:
    print(f"policy allowed: {event}")
    # 在这里执行一次页面导航、静态预检或 API 探测。

await page.wait_for_timeout(
    int(varied_wait_seconds(2.0, ratio=0.8, floor_seconds=0.5, ceiling_seconds=5.0) * 1000)
)
```

正式运行模板：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py --dry-run --max-jobs 5
python scripts/crawl_runner.py --max-jobs 3
```

针对单站点页面级取证：

```bash
source .venv/bin/activate
python scripts/ctf_resource_crawl.py \
  --sites zhihu \
  --max-image-save 3 \
  --max-scrolls 3 \
  --behavior-profile social_high_risk \
  --settle-min-ms 3000 \
  --settle-max-ms 9000
```

## 人类行为模拟

`scripts/human_flow.py` 是统一行为入口。现有 profile：

| profile | 适用场景 | 特征 |
|---|---|---|
| `social_high_risk` | 小红书、微博、抖音、知乎、B站等高压社交目标 | 停留更久、单批详情更少、评论区访问概率更高、CDP 触摸滚动概率更高。 |
| `travel_medium` | 携程、去哪儿、穷游等旅游内容页 | 阅读停留更长、滚动次数适中。 |
| `conservative` | 普通页面兜底取证 | 低频、稳定、较少额外交互。 |
| `quick_probe` | 本地快速确认，不用于正式取证 | 停留短、交互少。 |

在新 Playwright 脚本中接入行为模拟：

```python
from human_flow import (
    dwell_on_detail,
    dwell_on_list,
    install_runtime_hints,
    inter_detail_cooldown,
    load_behavior_profile,
)
from crawl_policy import site_request_guard
from web_sites import get_site

site = get_site("douyin")
profile = load_behavior_profile("social_high_risk", strict=True)

context = await playwright.chromium.launch_persistent_context(
    user_data_dir="data/browser_profiles_ctf/douyin",
    headless=False,
    locale="zh-CN",
    timezone_id="Asia/Shanghai",
    viewport={"width": 390, "height": 844},
    is_mobile=True,
    has_touch=True,
    args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
    ignore_default_args=["--enable-automation"],
)
await install_runtime_hints(context)

page = context.pages[0] if context.pages else await context.new_page()
with site_request_guard(site, label="manual-douyin-detail"):
    await page.goto(site.default_url, wait_until="domcontentloaded", timeout=60_000)

await dwell_on_detail(
    page,
    profile,
    content_hint={"body_text_length": 1200, "image_count": 8},
    max_scroll_passes=3,
)
await inter_detail_cooldown(profile)
```

关键要求：

- 使用持久化 `user_data_dir`，避免每次都是全新浏览器身份。
- 正式运行默认 `headless=False`，只有本地快速检查或无界面环境才加 `--headless`。
- 移动端站点使用移动 viewport、移动 UA、`is_mobile=True`、`has_touch=True`，并允许 CDP 触摸滚动。
- 详情页停留时间随正文长度和图片数量放大，避免短时间连续打开多个内容页。
- 评论区访问用概率控制，不要每次都点，也不要永远不点。

## 浏览器指纹与运行态覆盖

页面级取证入口 `ctf_resource_crawl.py` 已经集中处理浏览器上下文：

- 随机桌面或移动 viewport。
- `zh-CN` locale 和 `Asia/Shanghai` 时区。
- 桌面或移动 UA。
- `--disable-blink-features=AutomationControlled`。
- 忽略默认 `--enable-automation`。
- 注入 `navigator.webdriver`、`navigator.languages`、`navigator.plugins`、`navigator.platform`、`hardwareConcurrency`、`deviceMemory`、`window.chrome` 等 runtime override。
- 按 `web_sites.py` 的 `preferred_engine` 在 Playwright 和 Patchright 间选择；知乎当前走 `patchright` 和 `data/browser_profiles_stealth/zhihu`。

因此后续脚本优先调用现有入口，而不是新建裸 Playwright：

```bash
source .venv/bin/activate
python scripts/ctf_resource_crawl.py \
  --urls "https://www.zhihu.com/question/538549565" \
  --site-label zhihu \
  --max-image-save 3 \
  --max-scrolls 3 \
  --behavior-profile social_high_risk
```

如果必须新增脚本，至少复用这些函数：

```python
from ctf_resource_crawl import install_runtime_overrides, open_context
from human_flow import install_runtime_hints

context = await open_context(playwright, target, args)
await install_runtime_overrides(context)
await install_runtime_hints(context)
```

## 静态预检与浏览器取证的组合

对抗行为分析时，能用静态预检拿到的内容，不要立刻升级成完整浏览器会话。`ctf_resource_crawl.py --scrapling-preflight auto` 会对适合的目标先走 `scripts/ctf_scrapling_preflight.py`：

- 使用 Scrapling `Fetcher`。
- 对移动分享页设置移动 UA 和 referer。
- 记录结构化标记、captcha/verify 标记和 flag-like 文本。
- 仍然经过 `site_request_guard()`，不会绕开站点节流。

模板：

```bash
source .venv/bin/activate
python scripts/ctf_resource_crawl.py \
  --sites douyin \
  --scrapling-preflight auto \
  --max-image-save 2 \
  --max-scrolls 2
```

当 `summary.json` 中 `scrapling_preflight.ok=true` 且已有目标文本、图片或 flag-like 命中时，优先复用该产物；只有需要截图、运行态 JS、可见文本差异或图片样本时再使用浏览器阶段。

## 登录态、Cookie 与 Profile

反自动化检测经常把“全新 profile 高频访问”“频繁清空 cookie”“登录态突变”作为异常。项目约定：

- MediaCrawler 平台使用其自己的浏览器数据目录和 cookie 快照。
- 页面级 CTF 抓取使用 `data/browser_profiles_ctf/<site>`。
- 高压或 stealth 站点可以在 `web_sites.py` 设置 `profile_dir_override`，例如知乎使用 `data/browser_profiles_stealth/zhihu`。
- 除非站点已有明确污染问题，不要每轮清空全部 cookie。

### 小红书登录态保持经验

本次小红书靶场验证中，浏览器页面层和 profile 磁盘层出现过不一致：扫码登录成功时，运行中的 Playwright/CDP context 能看到 `web_session`、`a1`、`webId`、`gid`，页面左侧也能看到“我”；但关闭窗口后，`tools/MediaCrawler/browser_data/xhs_user_data_dir/Default/Cookies` 可能仍然是空表，下一次新窗口又进入安全扫码或普通登录流程。

因此小红书不能只依赖 Chromium profile 目录或 SQLite `Cookies` 文件。当前项目采用两层保持机制：

| 层级 | 文件/入口 | 作用 |
|---|---|---|
| 可见 preflight | `scripts/mediacrawler_batch_validate.py --xhs-preflight-only` 或完整批量任务的 xhs preflight | 打开可见窗口前先恢复 storage snapshot，随后等待安全扫码、普通登录和页面稳定；保存截图与 marker。 |
| storage snapshot | `tools/MediaCrawler/browser_data/xhs_user_data_dir/trippostcollect_storage_state.json` | 登录成功后导出 cookies、localStorage、sessionStorage；后续 MediaCrawler 新窗口启动时先恢复，再创建 API client。 |

修正后的流程是：preflight 和正式 MediaCrawler CDP 窗口都读取同一份 `trippostcollect_storage_state.json`。如果 snapshot 仍有效，preflight 会在导航前注入 cookies、localStorage 和 sessionStorage，通常不再需要重复扫码；如果站点仍弹安全确认，则按截图和 marker 作为证据等待人工确认。preflight 成功后会再次覆盖写入最新 snapshot，正式抓取窗口随后复用这份状态。

判断登录成功时只认强信号：

- 页面 UI 出现 `/user/profile/` 下的“我”入口。
- MediaCrawler API 层 `XiaoHongShuClient.pong` 返回 `Login state result: True`。

不要把以下弱信号单独当作成功：

- `web_session` 从空变为有值。
- `a1/webId/gid` 存在。
- 安全扫码页消失但页面仍弹出普通登录框。

这次视觉证据确认过的典型链路：

| 阶段 | 现象 | 判断 |
|---|---|---|
| 安全页 | “保护账号安全，请使用已登录该账号的小红书 APP 扫码验证身份” | 还不是登录成功。 |
| 回到首页 | 首页出现普通登录框，左侧仍是“登录” | `web_session` 可能已有，但仍未登录。 |
| 成功 | 左侧出现“我”，页面有“登录成功”提示 | 可以导出 storage snapshot。 |

刷新小红书登录态并只生成 snapshot：

```bash
source .venv/bin/activate
python scripts/mediacrawler_batch_validate.py \
  --xhs-preflight-only \
  --platforms xhs \
  --target-count 10 \
  --batch-size 10 \
  --login-type cookie \
  --xhs-preflight-timeout 300 \
  --xhs-initial-delay-seconds 8 \
  --xhs-screenshot-interval 5
```

验证新窗口能不扫码恢复登录态：

```bash
source .venv/bin/activate
python scripts/mediacrawler_batch_validate.py \
  --keyword 济南旅游 \
  --platforms xhs \
  --target-count 10 \
  --batch-size 10 \
  --login-type cookie \
  --timeout-per-batch 900 \
  --skip-xhs-preflight \
  --xhs-initial-delay-seconds 8 \
  --xhs-login-wait-seconds 30
```

成功日志应至少包含：

```text
Restored ... cookies from storage state
Installed storage restore init script
Login state result: True
Wrote storage state snapshot
```

本次修复的可复查证据：

- `outputs/mediacrawler_batch_validation/20260706T100008+0000/summary.json`：登录成功后导出 snapshot，记录 cookie 名、origin 数和截图。
- `outputs/mediacrawler_batch_validation/20260706T100307+0000/summary.json`：跳过 preflight 后，新窗口直接恢复登录态并抓到 10 条有效小红书图文。
- `outputs/mediacrawler_batch_validation/20260706T100307+0000/xhs/batch_01/logs/stderr.log`：包含 storage restore、`pong=True` 和多页补足图文记录。

抖音例外：当前脚本会在页面级抓取前后清理抖音相关 cookie，避免陈旧挑战态、异常风控态污染后续取证。该行为由 `--douyin-cookie-cleanup auto` 控制：

```bash
source .venv/bin/activate
python scripts/ctf_resource_crawl.py \
  --sites douyin \
  --douyin-cookie-cleanup auto \
  --behavior-profile social_high_risk
```

刷新 MediaCrawler 登录态：

```bash
source .venv/bin/activate
python scripts/mediacrawler_login_warmup.py \
  --platforms zhihu \
  --timeout-seconds 600
```

低频运行 MediaCrawler：

```bash
source .venv/bin/activate
python scripts/mediacrawler_crawl.py \
  --platforms xhs \
  --keyword 济南旅游 \
  --login-type cookie \
  --headed \
  --download-images \
  --timeout-per-platform 420
```

## 失败分类与冷却

`scripts/failure_classifier.py` 会把验证码、安全验证、登录要求、阻断和普通失败分开。调度器遇到 `blocked`、`login_required`、`captcha_detected`、`failed_final` 会进入失败处理路径。脚本中如果确认遇到风控或验证码，应调用 `record_site_cooldown()`，不要立即重试：

```python
from crawl_policy import record_site_cooldown
from web_sites import get_site

record_site_cooldown(
    get_site("xhs"),
    reason="captcha_detected",
    evidence=["visible text contains 安全验证", "screenshot saved"],
)
```

冷却记录会写入 `data/runtime/scrapling_throttle.json`，后续 `site_request_guard()` 自动阻断该站点直到冷却结束。

## 后续利用中的稳定规避清单

正式执行前：

- 用 `crawl_runner.py --dry-run` 确认任务、headed/headless、profile 和命令参数。
- 确认 `config/crawl_targets.json` 的 `schedule_seconds` 大于站点 `min_delay_seconds`，高压站点至少按小时级或天级运行。
- 对高压站点使用 `social_high_risk`，对旅游内容页使用 `travel_medium`。
- 不使用 `--no-throttle`，不把 `max_scrolls`、`max_image_save`、`max_notes` 一次性调得过高。

执行中：

- 单进程串行跑站点，不并发打同一 host。
- 先静态预检，再浏览器取证。
- 详情页之间保留 `inter_detail_cooldown()`。
- 发生验证码、验证页、403 或明显阻断时立即记录冷却并停止该站点。

执行后：

- 查看 `outputs/ctf_resource_crawls/<batch>/summary.json` 的 `policy_events`、`behavior_events`、`browser_engine`、`media_policy`。
- 查看 `data/runtime/scrapling_throttle.json` 确认每日预算、会话计数和冷却状态。
- 如果失败率升高，先降低 `daily_request_budget`、增大 `min_delay_seconds` 和 `cooldown_minutes`，再考虑改指纹覆盖。

快速检查命令：

```bash
source .venv/bin/activate
python -m json.tool data/runtime/scrapling_throttle.json >/dev/null
find outputs/ctf_resource_crawls -maxdepth 2 -name summary.json -print | tail -5
```

提取最近一次行为证据：

```bash
source .venv/bin/activate
python - <<'PY'
import json
from pathlib import Path

summaries = sorted(Path("outputs/ctf_resource_crawls").glob("*/summary.json"))
if not summaries:
    raise SystemExit("no ctf_resource_crawl summaries")
summary = json.loads(summaries[-1].read_text(encoding="utf-8"))
for record in summary.get("records", []):
    print(record["site"], record.get("browser_engine"), record.get("behavior_profile"))
    print("policy_events:", len(record.get("policy_events", [])))
    print("behavior_events:", [item.get("event") for item in record.get("behavior_events", [])[:8]])
    print("blocked:", bool(record.get("blocked_by_policy")), "nav_error:", record.get("nav_error", ""))
PY
```

## 可调参数建议

| 场景 | 建议 |
|---|---|
| 出现验证码或安全验证 | 记录冷却，`cooldown_minutes` 提到 180 以上，下一轮 headed 人工确认登录态。 |
| 页面加载正常但图片少 | 不急着增加滚动；先提高 `settle-max-ms` 和 `max_image_save`，保留 `max_scrolls <= 3`。 |
| 频繁 `networkidle` 超时 | 保留现有内容就绪判断，不把超时当作失败；检查 `navigation.readiness`。 |
| 同站点多目标利用 | 用 `inter_detail_cooldown()` 串行处理，`max_details_per_batch` 不超过 profile 默认值。 |
| 需要更强隐蔽性 | 优先增加间隔和降低日预算；其次使用站点专属持久 profile；最后才调整 runtime override。 |

这套方案的目标不是高速抓取，而是在授权靶场中稳定复现页面、保留证据，并让后续利用不会因为固定节奏、空白 profile 或机械交互被行为分析提前阻断。
