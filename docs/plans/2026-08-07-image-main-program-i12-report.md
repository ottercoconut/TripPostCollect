# 图片本地存储主程序 I-12 验收报告

前置根项目提交：`df82d0e`（I-11）

综合回归期间形成的修复提交：

- 根项目 `e233082`：兼容 B站 `bili/image_manifest.jsonl` 相对当前目录的 staging 布局，同时保留
  MediaCrawler 四个平台相对数据根目录的布局。
- 根项目 `f59ad75`：清理全量 Ruff 检查发现的既有未使用导入。
- MediaCrawler `6e7475d4a3501a346ec0eccb592f5598b9b9718b`：解析抖音真实搜索接口返回的
  HTTP chunk framing 多 JSON 文档流；保持任一业务错误和 `verify_check` 不被后续文档覆盖。

## 根项目全量回归

```text
source .venv/bin/activate
python -m pytest
234 passed

python -m ruff check src scripts tests apps/admin_api
All checks passed

python -m compileall -q src scripts tests
passed

python scripts/verify_frozen_files.py
Frozen file verification passed
```

全量 pytest 包含五平台 fixture 的临时 SQLite、临时媒体根、manifest、晋升、幂等重跑和 runner
门禁测试。测试没有写默认库或 `data/media/`。

## MediaCrawler 回归

完整命令 `uv run pytest -q` 共收集 176 个测试，结果为：

- 162 passed；
- 8 skipped：全部为 `test/test_mongodb_integration.py`，原因是本机没有 MongoDB；
- 6 failed：`test/test_proxy_ip_pool.py` 3 项和 `test/test_redis_cache.py` 3 项，均明确为
  `127.0.0.1:6379 Connection refused`，本机没有 Redis；
- 图片、五平台适配、登录、行为、发现记忆和抖音流式解析测试 0 失败。

排除上述两个外部服务集成文件后运行：

```text
uv run pytest -q \
  tests \
  test/test_db_sync.py \
  test/test_expiring_local_cache.py \
  test/test_utils.py
160 passed
```

抖音流式解析专项另为 25 passed。全量测试仍有 SQLAlchemy 2.0 的既有弃用 warning，不影响本步
图片接口和完成谓词。

## 五平台真实 `--no-import --download-images` 小样

所有样本均使用临时 SQLite 或 `--no-import`，候选上限不超过 10；只生成 staging、JSONL、manifest
和诊断状态，不晋升到长期媒体根，不写发现 checkpoint。表中的图片数已经由根项目重新读取文件、
校验受控路径、真实 MIME、尺寸和 SHA-256，不只采信平台摘要。

| 平台 | 候选上限 / 实际候选 | 完成图文 | manifest / 文件 / 根校验 | 视频文件 | 顶层结果 |
|---|---:|---:|---:|---:|---|
| B站 | 1 / 1 | 1 | 3 / 3 / 3 | 0 | completed |
| 微博 | 10 / 1 | 1 | 14 / 14 / 14 | 0 | completed |
| 抖音 | 10 / 3 | 1 | 4 / 4 / 4 | 0 | completed |
| 知乎 | 3 / 3 | 1 | 14 / 14 / 14 | 0 | completed |
| 小红书 | 10 / 6 | 1 | 6 / 6 / 6 | 0 | completed |

最终通过摘要：

- B站：`temp/i12-real/bilibili-image/20260807T113234955200+0000/summary.json`；
- 微博：`temp/i12-real/weibo-image/20260807T112756554763+0000/summary.json`；
- 抖音：`temp/i12-real/douyin-image/20260807T112513232011+0000/summary.json`；
- 知乎：`temp/i12-real/zhihu/20260807T104430892222+0000/summary.json`；
- 小红书 child：
  `outputs/xhs_runs/20260807T111936357921+0000/20260807T111936462247+0000/summary.json`；
- 小红书顶层：`data/runtime/xhs/runs/20260807T111936357921+0000/run_summary.json`；
- 小红书冻结状态：
  `data/runtime/xhs/execution_states/20260807T111936357921+0000/i12_real_sample.json`。

小红书使用 `xhs_runner.py`、显式账号、独立临时 profile、加密 storage state、`xhs_guarded` 和
`post_interaction=none`。过期登录态先经 `xhs_login.py` 人工扫码，并通过关闭/重开同一 profile
身份复验；随后重新复制到临时目录。最终小样在 6 个候选内找到 1 条有效图文，五阶段中
`persistence_verified=skipped` 仅因为本轮显式 `--no-import`，顶层 `task_finalized=completed`，且
`discovery.skipped.reason=no_import`。此前三个候选全为视频的诊断轮正确得到 0 图片并失败于完成
门禁，没有被当作最终通过证据。

抖音首轮真实响应揭示一个 chunk-framed 多 JSON 文档流，修复后先以真实搜索结果证明解析成功，
再用“青岛旅游图文攻略”在 3 个候选中跳过 2 个视频并取得 1 个图文。微博此前含成功图片但后续
请求超时的轮次也未作为最终证据，最终重跑为退出码 0 的完整单帖样本。B站在修复 staging 根目录
判定后重新运行，顶层摘要直接通过，不再依赖对旧摘要的离线解释。

## 无用图片和媒体边界

五份最终 manifest 的 `image_role` 全部为 `content`，实际来源键只有平台白名单：

- B站 `image_urls`；
- 微博、知乎、小红书 `image_list`；
- 抖音 `note_download_url`，由 `images[]` 的单一权威下载变体投影而来。

真实样本的 `video_file_count=0`，`promoted_images=0`。头像、作者主页图片、搜索预览、封面、视频、
音乐和知乎公式图片均不进入 manifest；其零调用边界由 I-01 投影测试和 I-05 至 I-09 平台 spy
测试覆盖。抖音与小红书真实样本还实际包含被跳过的视频候选，证明不会把视频封面降级为正文图。

## 默认数据不变量

最终只读复验结果：

| 项目 | I-00 基线 | I-12 结果 |
|---|---:|---:|
| 默认库 SHA-256 | `a7f78d3636025d61b442b50050afb2c36b7808772131d77df1c344bcdf371006` | 相同 |
| 默认库大小 | 143,273,984 bytes | 相同 |
| `web_posts` | 6,719 | 6,719 |
| `web_post_images` | 63,477 | 63,477 |
| 带本地路径关系 | 17,150 | 17,150 |
| 唯一本地路径 | 17,150 | 17,150 |
| 本地文件缺失 | 0 | 0 |
| 数据库 SHA 与文件 SHA 不一致 | 0 | 0 |
| 路径逃逸 | 0 | 0 |
| `data/media/` 文件 | 0 | 0 |
| `PRAGMA quick_check` | ok | ok |
| 外键违规 | 0 | 0 |

平台帖子数仍为 B站 3,006、微博 1,007、抖音 461、知乎 437、小红书 1,808。登录刷新只更新账号
profile 和加密会话快照；默认内容库、正式媒体根、现有图片关系和历史文件未修改。

## 结论

`I_12_MULTIPLATFORM_REGRESSION_READY=true`

I-12 的代码、fixture、五平台真实小样和默认数据不变量均通过。Redis/Mongo 项是明确的可选外部
服务环境缺失，不是图片代码失败；进入 I-13 时仍不得执行任何 H 阶段历史补全。
