# TripPostCollect 本地管理端

管理端用于只读查看 `data/trippostcollect.sqlite` 中的内容记录、正文图片、页面证据、数据质量和抓取
报告。它不是抓取入口，不执行 bootstrap、配置同步、dry-run 或正式抓取，也不修改内容表。

## 组成

| 目录 | 技术 | 职责 |
|---|---|---|
| `apps/admin_api/` | FastAPI、`sqlite3` | 只读查询、图片/证据文件代理、静态前端托管 |
| `apps/admin_web/` | React、TypeScript、Vite | 记录工作台、详情、数据质量和运行状态界面 |

后端默认读取项目主库和 `config/crawl_targets.json`。前端生产构建位于
`apps/admin_web/dist/`，存在构建产物时由 FastAPI 同一进程托管；开发期由 Vite 把 `/api` 代理到
`127.0.0.1:8787`。

## 产品边界

- 以 `web_posts` 为内容主表，以 `web_post_images` 为正文图片子表。
- `ctf_captures` 和 `ctf_capture_images` 只作为证据与调试上下文展示。
- 不提供记录或图片的创建、修改、隐藏、排序、删除和重导入。
- 不从浏览器触发抓取、调度同步、数据库维护或 shell 命令。
- 不编辑 `source_platforms`、`crawl_jobs` 或抓取配置。
- 不恢复已从数据模型移除的 `city_name`；使用平台、关键词、状态和时间筛选。
- 文件预览必须按数据库 ID 反查，并限制在项目允许的根目录内；不得接受任意本地路径。
- 默认仅监听 `127.0.0.1`，不按公网服务设计。

这些边界由后端依赖层、路由实现和测试共同约束。新增写操作或命令执行能力必须单独设计权限、备份、
审计与回滚，不能通过放宽现有环境变量直接启用。

## 运行

### Docker（常驻运行）

```bash
docker compose \
  -f compose.admin.yaml \
  up \
  --build \
  -d
```

浏览器访问 `http://127.0.0.1:8787`。容器使用只读根文件系统、非 root 用户、只读项目挂载、无额外
Linux capabilities，并固定关闭管理命令。

查看状态或停止：

```bash
docker compose \
  -f compose.admin.yaml \
  ps
```

```bash
docker compose \
  -f compose.admin.yaml \
  down
```

### 本地开发

后端：

```bash
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m uvicorn apps.admin_api.app.main:app \
  --reload \
  --host 127.0.0.1 \
  --port 8787
```

前端在另一个终端运行：

```bash
cd apps/admin_web
npm ci
npm run dev
```

生产前端构建：

```bash
cd apps/admin_web
npm ci
npm run build
```

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `TRIPPOST_ADMIN_DB` | `data/trippostcollect.sqlite` | SQLite 数据库路径 |
| `TRIPPOST_ADMIN_CONFIG` | `config/crawl_targets.json` | 调度配置路径 |
| `TRIPPOST_ADMIN_STATIC_DIR` | `apps/admin_web/dist` | 前端构建目录 |
| `TRIPPOST_ADMIN_REFRESH_SECONDS` | `5` | 前端建议刷新间隔 |

Compose 会把数据库和配置解析为宿主机绝对路径，再以只读方式挂载整个项目。不要把数据库复制进镜像，
否则管理端无法看到抓取进程的实时提交。

监听地址由启动入口决定：本地开发使用上文 Uvicorn 的 `--host/--port`，Compose 只读取宿主机环境中的
`TRIPPOST_ADMIN_PORT` 来映射宿主端口，容器内始终监听 `0.0.0.0:8787`。settings 中保留的
`TRIPPOST_ADMIN_HOST` / `TRIPPOST_ADMIN_PORT` 不控制这两个现有入口。

`TRIPPOST_ADMIN_DB_READONLY` 和 `TRIPPOST_ADMIN_ALLOW_COMMANDS` 当前只出现在服务元数据中，不是安全
开关：数据库连接始终只读，代码也没有命令执行路由。不要通过修改这两个变量尝试启用写库或命令；
新增能力必须实现并测试新的授权边界。

## API 分区

| 前缀 | 内容 |
|---|---|
| `/api/health`、`/api/meta` | 服务、数据库和运行元数据 |
| `/api/records` | 记录列表、详情上下文和原始字段 |
| `/api/images` | 正文图片代理 |
| `/api/captures` | 页面证据、证据图片和白名单 artifact |
| `/api/overview` | 计数、字段缺口和近期运行 |
| `/api/platforms` | 平台元数据 |
| `/api/scheduler` | 配置、任务和运行报告的只读视图 |
| `/api/maintenance/schema-status` | schema 状态检查 |

OpenAPI 页面位于 `/api/docs`。路由新增时应继续使用显式查询、强制分页和统一响应格式，不向前端泄漏
未校验的绝对路径或任意文件读取能力。

## 数据与安全约束

- SQLite 使用短生命周期只读连接；请求处理不得调用 bootstrap 或执行迁移。
- 图片和证据读取通过共享 artifact 路径策略校验，拒绝项目外路径及符号链接逃逸。
- JSON 解析失败只返回错误信息，不修写原始字段。
- CORS 只允许本地 Vite 开发地址，生产模式使用同源静态托管。
- 列表接口必须分页；前端不得一次加载全部记录或图片。
- 抓取与管理端并行时，只展示 SQLite 已提交数据，不读取 staging 目录推断成功。

## 验证

后端：

```bash
source .venv/bin/activate
python -m pytest apps/admin_api/tests
```

前端：

```bash
cd apps/admin_web
npm run build
```

涉及路径、安全或只读边界的改动，至少验证：数据库不会被 bootstrap 或写入、项目外文件不可读取、
图片和证据只能通过数据库记录访问、SPA 路由不会吞掉 `/api/*` 的 404。

## 维护原则

抓取流程、完成语义和入库契约属于 `docs/`；本文件只维护管理端的运行、接口和安全边界。已经完成的
产品设计过程、阶段提示和临时验收数据从 Git 历史查询，不在现行维护文档中重复保存。
