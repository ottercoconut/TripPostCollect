# TripPostCollect 可视化管理客户端开发文档

## 目标

建设一个面向本项目 SQLite 数据库的本地/内网管理台，以“记录”为核心管理图文内容。用户先通过平台、城市名、关键词、状态和时间等条件筛选出符合条件的记录，再围绕选中记录查看图片预览、作者信息、互动指标、页面证据和原始 JSON。客户端不是新的抓取器，不绕过现有 `crawl_runner.py`、`mediacrawler_crawl.py`、`ctf_resource_crawl.py` 和入库脚本；它只提供清晰、可审计的管理界面。

首版采用正式产品化技术栈：

- 后端：FastAPI + Python 标准 `sqlite3`，必要时引入 Pydantic 模型。
- 前端：React + TypeScript + Vite。
- UI：自建轻量组件层，优先使用成熟表格和表单库；视觉风格保持运维后台式、信息密度高、克制。
- 数据库：继续使用 `data/trippostcollect.sqlite`，不改变现有抓取入口。

官方技术依据：

- FastAPI 可与任意 SQL/NoSQL 数据库库配合；官方 SQL 数据库教程示例使用 SQLModel/SQLAlchemy/Pydantic，并说明 SQLite 是单文件、Python 集成支持的数据库选择。
- Vite 是现代前端构建工具，提供开发服务器和生产构建；官方模板支持 `react-ts`。
- React 官方建议生产应用从框架开始；本项目选择 React + Vite 是因为管理台主要是本地 SPA，后端已由 FastAPI 承担。

## 非目标

- 不重新实现抓取逻辑。
- 不直接从 UI 高频触发线上平台抓取。
- 不从 UI 触发真实抓取、sync 或 dry-run；抓取和调度仍从命令行进入。
- 不把 `ctf_captures` 当作普通内容表随意修改。
- 不在 UI 中手工新增记录；记录新增只来自现有抓取和入库链路。
- 不在首版实现多用户协作、远程公网部署、复杂 RBAC 或 OAuth。
- 不让前端直接访问 SQLite 文件或本地任意路径。
- 不在 UI 中编辑 `source_platforms` 数据库行；重构前平台注册源头仍是 `scripts/web_sites.py`，重构后迁移到 `trippostcollect.platforms.registry`。
- 不在 UI 中直接编辑 `crawl_jobs` 行；调度源头仍是 `config/crawl_targets.json`，同步入口仍是命令行 `crawl_runner.py --sync-only`。

## 设计原则

1. 管理台第一入口是记录筛选和记录工作台，不是数据库表导航。
2. `web_posts` 是记录主表；产品语言统一称为“记录”，表名只作为实现细节出现。
3. 平台和城市名是首要筛选维度，允许只按城市名筛选，也允许平台 + 城市组合筛选。
4. 图片、作者、互动指标、证据和 JSON 都是选中记录的上下文面板。
5. `web_post_images` 是记录图片子表，首版允许排序、标记、替换 URL 和删除错误图片行；新增图片默认由入库链路完成。
6. `ctf_captures` 和 `ctf_capture_images` 是证据/调试底座，默认只读；通常从关联记录进入。
7. 调度配置和运行报告在管理端只读；修改配置、sync、dry-run 和真实抓取继续使用命令行。
8. 图片读取必须经过后端代理，后端只允许读取项目目录内白名单路径。
9. 所有写操作必须记录变更审计。
10. 记录删除只做软删除；图片行删除不删除磁盘文件，磁盘清理另做维护工具。
11. 保留原始 `raw_sample_json`、`raw_meta_json`、HTML、截图和图片证据。
12. `published_at` 只能表示平台原始发帖时间；UI 不提供“一键用抓取时间填充”的功能。
13. 视频记录仍不进入内容主表；UI 只展示跳过原因或证据，不提供视频处理能力。

## 核心对象：记录

管理台的核心对象是“记录”。一条记录以 `web_posts` 的一行为主体，并按需聚合以下信息：

- 平台信息：`source_platforms`。
- 图片信息：`web_post_images`。
- 作者信息：`web_posts` 的作者字段和 `author_json`。
- 互动指标：点赞、收藏、评论、转发、浏览量和 `metrics_json`。
- 证据信息：`source_capture_id` 关联的 `ctf_captures`。
- 证据图片：关联证据下的 `ctf_capture_images`。
- 原始数据：`raw_sample_json`、`raw_meta_json`、`artifact_dir`、截图、HTML、可见文本。

前端不应该把 `web_posts`、`web_post_images`、`ctf_captures` 暴露成并列的主工作区。它们在界面上应被组织为：

```text
筛选条件 -> 记录列表 -> 选中记录 -> 图片 / 作者 / 内容 / 互动 / 证据 / JSON
```

只有调度、运行报告、系统设置这类不依附于单条记录的能力，才作为独立导航入口。

## 产品范围

### 用户

首版面向单人使用：项目维护者或数据整理者。

典型任务：

- 选择平台、城市名，或只输入城市名，筛选出符合条件的记录。
- 在记录列表中快速判断平台、标题、作者、城市、发布时间、图片数和状态。
- 打开一条记录后查看其图片预览、作者信息、正文、互动指标、证据和原始 JSON。
- 找出缺图片、缺发布时间、缺作者粉丝量的记录。
- 编辑记录标题、正文、作者字段、互动数、城市、关键词和状态。
- 打开记录对应截图、HTML、可见文本和 JSON 原始证据。
- 查看 `crawl_targets.json` 中的长期任务和当前任务状态。
- 查看运行报告，不从前端触发 sync、dry-run 或真实抓取。

### 工作区

| 工作区 | 作用 | 首版要求 |
|---|---|---|
| 记录工作台 | 按平台、城市、关键词、状态、时间筛选记录，并显示记录列表 | 必做，默认首页 |
| 记录详情 | 围绕单条记录展示图片、作者、内容、互动、证据和 JSON，并提供白名单字段 CRUD | 必做 |
| 数据质量 | 以记录为单位查看缺图片、缺发布时间、缺作者粉丝量等问题 | 必做 |
| 图片浏览 | 从记录进入图片墙、失效图片、重复图片初筛 | 必做，作为记录上下文 |
| 证据查看 | 从记录进入关联 `ctf_captures`；允许独立查询证据 | 必做，只读 |
| 调度配置 | 查看 `config/crawl_targets.json` 和同步后的任务状态 | 必做，只读 |
| 运行报告 | 查看 `crawl_run_reports` 和摘要文件 | 必做，只读 |
| 系统设置 | DB 路径、只读模式、备份、维护命令 | 二期 |

## 信息架构

```text
TripPostCollect Admin
  记录工作台
    筛选器
      平台
      城市
      关键词
      状态
      发布时间
      抓取时间
      缺字段
    记录列表
    记录详情
      图片预览
      作者信息
      内容正文
      互动指标
      证据与原始数据
      审计日志
  数据质量
    缺图片记录
    缺发布时间记录
    缺作者字段记录
    批量编辑
  图片浏览
    当前记录图片
    失效图片初筛
    重复图片初筛
  证据
    关联证据
    独立证据查询
    证据详情
  调度
    任务配置
    Dry-run 预览
    运行报告
  设置
    数据库
    备份
    审计日志
```

## 前端静态模板

首版视觉方向先以静态模板固定：本机后台、记录工作台、高密度表格、右侧记录上下文，不做营销页，不做表级数据库浏览器。

- HTML 模板：[admin-client-record-workbench-template.html](admin-client-record-workbench-template.html)。
- PNG 预览：[assets/admin-client-record-workbench.png](assets/admin-client-record-workbench.png)。

![记录工作台静态模板](assets/admin-client-record-workbench.png)

## 技术架构

```text
apps/admin_web
  React + TypeScript + Vite
  -> HTTP JSON API

apps/admin_api
  FastAPI
  -> trippostcollect.records
  -> trippostcollect.db
  -> trippostcollect.artifacts
  -> trippostcollect.scheduler

src/trippostcollect
  共享领域代码
  -> sqlite3
  -> config/crawl_targets.json
  -> outputs/ and data/runtime summaries

data/trippostcollect.sqlite
outputs/
data/runtime/
config/crawl_targets.json
```

后端是唯一允许访问 SQLite、配置文件和本地证据路径的进程。前端只通过 API 获取数据和图片。现有抓取脚本和新管理台不应该各自实现一套数据库访问、路径解析、平台映射或记录聚合逻辑；这些能力必须下沉到 `src/trippostcollect/`。

## 项目结构重构方案

当前仓库以“收集数据并入库”为中心，`scripts/` 同时承担 CLI、领域逻辑、数据库访问、路径解析和外部工具编排。管理端产品化后，需要把项目拆成四层：

- `apps/`：可运行应用，包括管理端 API 和管理端 Web。
- `src/trippostcollect/`：项目共享 Python 包，承载数据库、记录、证据、调度、抓取编排等领域代码。
- `scripts/`：保留用户已有命令入口，但只做参数解析和调用共享包，不再沉淀新业务逻辑。
- `data/`、`outputs/`、`temp/`、`tools/`：继续作为运行数据、证据产物、临时验证和第三方工具目录，不混入应用源码。

### 目标目录

```text
apps/
  admin_api/
    app/
      __init__.py
      main.py
      deps.py
      settings.py
      schemas/
      routers/
      services/
    tests/
  admin_web/
    index.html
    package.json
    tsconfig.json
    vite.config.ts
    src/
      api/
      components/
      features/
      layouts/
      routes/
      styles/
      types/

src/
  trippostcollect/
    __init__.py
    core/
      paths.py
      settings.py
      time.py
      json_utils.py
      subprocesses.py
    db/
      connection.py
      bootstrap.py
      migrations.py
      repositories/
    platforms/
      registry.py
      models.py
    records/
      schemas.py
      repository.py
      service.py
      images.py
      quality.py
    artifacts/
      paths.py
      image_proxy.py
      evidence_reader.py
    scheduler/
      config.py
      runner.py
      reports.py
    crawling/
      policy.py
      human_flow.py
      media_crawler.py
      ctf_capture.py
      failure_classifier.py
    imports/
      mediacrawler_import.py
      ctf_import.py

scripts/
  crawl_runner.py
  mediacrawler_crawl.py
  mediacrawler_login_warmup.py
  mediacrawler_batch_validate.py
  ctf_resource_crawl.py
  import_ctf_captures.py
  project_paths.py
  db_bootstrap.py
  web_sites.py

db/
  *.sql
config/
  crawl_targets.json
docs/
  admin-client-development.md
```

### 目录职责

| 目录 | 职责 | 迁移策略 |
|---|---|---|
| `apps/admin_api/` | FastAPI 管理端，只处理 HTTP、依赖注入、响应模型和权限边界 | 新增 |
| `apps/admin_web/` | React/Vite 管理端前端 | 新增 |
| `src/trippostcollect/core/` | 路径、时间、配置、JSON、子进程固定命令模板 | 从 `scripts/project_paths.py` 和零散工具函数抽取 |
| `src/trippostcollect/db/` | SQLite 连接、bootstrap、迁移、通用 repository 基础能力 | 从 `scripts/db_bootstrap.py` 抽取 |
| `src/trippostcollect/platforms/` | 平台注册、平台 key、平台展示名、默认 URL 和风险配置 | 从 `scripts/web_sites.py` 抽取 |
| `src/trippostcollect/records/` | 记录列表、记录详情上下文、图片、作者、互动、质量检查 | 新增，管理端优先使用 |
| `src/trippostcollect/artifacts/` | 本地文件白名单、图片代理、截图/HTML/文本读取 | 从证据抓取和管理端需求中抽取 |
| `src/trippostcollect/scheduler/` | 任务配置、sync、dry-run、运行报告读取 | 从 `scripts/crawl_runner.py` 抽取 |
| `src/trippostcollect/crawling/` | 抓取策略、MediaCrawler 编排、页面证据抓取 | 从现有抓取脚本逐步抽取 |
| `src/trippostcollect/imports/` | MediaCrawler 和 CTF 证据入库、归一化 | 从导入脚本抽取 |
| `scripts/` | 兼容已有命令，保留当前用户操作习惯 | 逐步改为薄 wrapper |
| `db/` | SQL schema 仍作为数据库结构权威文件 | 保留根目录，避免和运行库混淆 |
| `tools/MediaCrawler/` | 第三方工具 | 不移动，不改成项目源码 |
| `data/`、`outputs/`、`temp/` | 数据库、浏览器状态、运行产物、临时验证 | 不移动，只通过 `core.paths` 访问 |

### 兼容入口

重构后，以下现有命令必须继续可用：

```bash
source .venv/bin/activate
python scripts/crawl_runner.py --sync-only
```

```bash
source .venv/bin/activate
python scripts/crawl_runner.py \
  --dry-run \
  --max-jobs 5
```

```bash
source .venv/bin/activate
python scripts/mediacrawler_crawl.py \
  --platforms xhs \
  --keyword 济南旅游 \
  --login-type cookie \
  --headed
```

这些脚本内部可以改为：

```python
from trippostcollect.scheduler.runner import main

if __name__ == "__main__":
    raise SystemExit(main())
```

也就是说，用户入口不变，业务实现位置迁移。

### 路径迁移规则

- `scripts/project_paths.py` 先保留，内部转发到 `trippostcollect.core.paths`。
- 新代码只允许引用 `trippostcollect.core.paths` 或后端 settings，不再直接引用 `scripts/project_paths.py`。
- 不移动 `data/`、`outputs/`、`temp/` 和 `tools/MediaCrawler/`，避免破坏既有运行产物、浏览器 profile 和第三方工具路径。
- 管理端文件代理必须通过 `trippostcollect.artifacts` 做白名单检查，不允许自行拼本地路径。
- 数据库 schema 文件继续留在 `db/`；`trippostcollect.db.bootstrap` 负责定位和执行 schema。

### 重构边界

首版不做一次性全仓库搬迁。迁移顺序应服务于管理端：

1. 先抽 `core.paths`、`db.bootstrap`、`platforms.registry`，让管理端和旧脚本共用基础能力。
2. 再实现 `records.repository` 和 `records.service`，支撑 `/api/records`。
3. 再抽 `artifacts.image_proxy` 和 `artifacts.evidence_reader`，支撑图片预览和证据查看。
4. 最后再逐步整理抓取和导入脚本，避免在管理端首版前大面积改动稳定抓取链路。

禁止在重构中做以下事情：

- 为了目录美观移动 `data/` 或 `outputs/` 中已有产物。
- 把 `tools/MediaCrawler/` 合并进项目包。
- 让 `apps/admin_api` 直接 import `scripts/*.py` 中的业务函数作为长期方案。
- 同时重写抓取逻辑和管理端逻辑。
- 修改数据库 schema 却不提供迁移和临时库验证。

## 后端技术设计

### 运行方式

开发期：

```bash
source .venv/bin/activate
python -m uvicorn apps.admin_api.app.main:app \
  --reload \
  --host 127.0.0.1 \
  --port 8787
```

生产/本机常驻期：

```bash
source .venv/bin/activate
python -m uvicorn apps.admin_api.app.main:app \
  --host 127.0.0.1 \
  --port 8787
```

### 依赖建议

Python 依赖：

- `fastapi`
- `uvicorn`
- `pydantic`
- `python-multipart`，用于未来上传图片或导入文件
- 暂不强制 ORM，首版直接用 `sqlite3.Row` + 显式 SQL，减少与既有 schema 演进的摩擦

是否引入 SQLAlchemy/SQLModel：

- 首版不引入。
- 原因：当前表来自 SQL 文件和 `ALTER TABLE` 演进，字段多、JSON 字段多、写入边界特殊，显式 SQL 更透明。
- 二期如果需要更复杂的模型校验，再评估 SQLModel。

### SQLite 连接策略

- 每个请求打开短连接，设置 `row_factory=sqlite3.Row`。
- 每个写请求使用事务。
- 启动时执行 `bootstrap_database()`，确保 schema 和平台注册/任务配置一致。
- 对写操作设置 `PRAGMA foreign_keys = ON`。
- 不开启长期持有的全局连接。

建议封装：

```python
def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn
```

### 配置

环境变量：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `TRIPPOST_ADMIN_DB` | `data/trippostcollect.sqlite` | SQLite 路径 |
| `TRIPPOST_ADMIN_CONFIG` | `config/crawl_targets.json` | 调度配置路径 |
| `TRIPPOST_ADMIN_HOST` | `127.0.0.1` | 服务监听 |
| `TRIPPOST_ADMIN_PORT` | `8787` | 服务端口 |
| `TRIPPOST_ADMIN_WRITE_ENABLED` | `true` | 是否允许写入 |
| `TRIPPOST_ADMIN_ALLOW_COMMANDS` | `false` | 首版固定为 false，不允许前端触发 sync、dry-run 或真实抓取 |
| `TRIPPOST_ADMIN_BACKUP_BEFORE_WRITE` | `true` | 写入前是否备份 DB |

所有路径必须用 `trippostcollect.core.paths` 或后端 settings 统一解析，不在业务代码里硬编码。迁移期允许 `scripts/project_paths.py` 作为兼容 wrapper 存在。

### 审计表

建议新增 `admin_audit_log`。这是管理台自己的审计表，不影响抓取主链路。

```sql
CREATE TABLE IF NOT EXISTS admin_audit_log (
    id INTEGER PRIMARY KEY,
    actor TEXT NOT NULL DEFAULT 'local_admin',
    action TEXT NOT NULL,
    table_name TEXT NOT NULL,
    row_id INTEGER,
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_log_table_row
ON admin_audit_log(table_name, row_id, id DESC);
```

所有 `POST`、`PATCH`、`DELETE` 必须写审计。批量操作写一条批量摘要，再按行写明细。

### 备份策略

默认每次写操作前按日创建 SQLite 备份：

```text
data/runtime/admin_backups/YYYYMMDD/HHMMSS_trippostcollect.sqlite
```

同一分钟多次写入可以复用同一个备份，避免文件膨胀。备份失败时，写操作失败。

### API 约定

统一响应：

```json
{
  "data": {},
  "meta": {},
  "errors": []
}
```

列表响应：

```json
{
  "data": [],
  "meta": {
    "page": 1,
    "page_size": 50,
    "total": 370,
    "sort": "-captured_at"
  },
  "errors": []
}
```

错误响应：

```json
{
  "data": null,
  "meta": {},
  "errors": [
    {
      "code": "validation_error",
      "message": "published_at must be an ISO datetime or null",
      "field": "published_at"
    }
  ]
}
```

### API 路由

#### Health

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/health` | 服务健康检查 |
| `GET` | `/api/meta` | DB 路径、写入开关、版本、表计数 |

#### Overview

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/overview/counts` | 总行数、平台分布 |
| `GET` | `/api/overview/field-gaps` | 缺字段统计 |
| `GET` | `/api/overview/recent-runs` | 最近运行摘要 |

#### Platforms

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/platforms` | 读取 `source_platforms` |

`source_platforms` 只读。若要改平台，迁移前修改 `scripts/web_sites.py`，迁移后修改 `trippostcollect.platforms.registry`。

#### Records

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/records` | 分页筛选记录 |
| `GET` | `/api/records/{id}` | 记录详情 |
| `GET` | `/api/records/{id}/context` | 记录上下文聚合数据 |
| `PATCH` | `/api/records/{id}` | 更新记录 |
| `DELETE` | `/api/records/{id}` | 默认软删除记录 |
| `POST` | `/api/records/bulk-update` | 批量更新记录 |

`/api/records` 是产品层主 API。实现上读取和写入 `web_posts`，但前端路由、组件和文案统一使用 `record`。若为了兼容早期脚本保留 `/api/posts`，也只能作为 `/api/records` 的别名，不作为文档和前端主入口。

首版不提供 `POST /api/records`。记录创建只由现有抓取和入库链路完成，管理端只负责筛选、查看、修正白名单字段、软删除和审计。

筛选参数：

- `platform_key`
- `city_name`
- `source_type`
- `status`
- `keyword`
- `published_from`
- `published_to`
- `captured_from`
- `captured_to`
- `has_images`
- `missing_published_at`
- `missing_author_followers`
- `q`
- `page`
- `page_size`
- `sort`

筛选行为：

- `city_name` 可以单独使用，不要求同时选择平台。
- `platform_key + city_name` 是首要组合筛选。
- `q` 用于标题、正文、作者名、平台帖子 ID 和 URL 的模糊查询。
- 缺字段筛选返回记录列表，不跳转到表级维护页面。

记录详情响应：

```json
{
  "data": {
    "record": {
      "id": 1,
      "platform_key": "xiaohongshu",
      "platform_name": "小红书",
      "title": "示例标题",
      "city_name": "上海",
      "keyword": "citywalk",
      "status": "captured"
    },
    "author": {
      "display_name": "作者名",
      "platform_id": "author_001",
      "profile_url": "https://example.com/u/author_001",
      "avatar_url": "https://example.com/avatar.jpg",
      "followers_count": 1200,
      "verified": false
    },
    "metrics": {
      "likes": 10,
      "favorites": 2,
      "comments": 1,
      "shares": 0,
      "views": null
    },
    "images": [],
    "capture": null,
    "artifacts": {},
    "raw": {
      "raw_sample_json": {},
      "metrics_json": {},
      "author_json": {}
    }
  },
  "meta": {},
  "errors": []
}
```

可编辑字段白名单：

- `platform_post_id`
- `source_url`
- `canonical_url`
- `title`
- `author_display_name`
- `author_platform_id`
- `author_profile_url`
- `author_avatar_url`
- `author_description`
- `author_followers_count`
- `author_following_count`
- `author_posts_count`
- `author_platform_level`
- `author_verified`
- `author_verified_text`
- `published_at`
- `city_name`
- `keyword`
- `content_text`
- `post_likes_count`
- `post_favorites_count`
- `post_comments_count`
- `post_shares_count`
- `post_reposts_count`
- `post_views_count`
- `metrics_json`
- `author_json`
- `status`

不可编辑字段：

- `id`
- `source_capture_id`
- `source_type`
- `captured_at`
- `raw_sample_json`
- `artifact_dir`
- `capture_method`
- `created_at`
- `updated_at`

如果后续确实需要人工补录记录，必须单独设计补录流程、来源标记、审计和 schema 约束，不混入首版记录工作台。

#### Record Images

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/records/{id}/images` | 记录图片列表 |
| `PATCH` | `/api/images/{id}` | 修改图片元数据 |
| `DELETE` | `/api/images/{id}` | 删除图片行 |
| `POST` | `/api/records/{id}/images/reorder` | 重排图片 |
| `GET` | `/api/images/{id}/preview` | 图片代理 |
| `GET` | `/api/images/by-url` | 远程图片代理 |

图片代理策略：

- 若 `local_path` 存在并位于项目根目录内，返回本地文件。
- 若没有本地文件，返回带签名/短时缓存的远程 URL 代理或直接返回 URL。
- 后端必须拒绝 `..`、绝对任意路径、项目根外路径。
- `web_post_images.image_role` 限定为 `content`、`page`、`author_avatar`。

#### Captures

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/records/{id}/capture` | 查看记录关联证据 |
| `GET` | `/api/captures` | 独立分页查看页面级证据 |
| `GET` | `/api/captures/{id}` | 证据详情 |
| `GET` | `/api/captures/{id}/images` | 证据图片 |
| `GET` | `/api/captures/{id}/artifact` | 读取白名单产物 |
| `POST` | `/api/captures/{id}/promote` | 重新归一化生成/更新 `web_posts` |

`ctf_captures` 默认不可编辑，并且不作为首屏主对象。允许的操作只有：

- 查看。
- 从记录详情进入关联证据。
- 重新导入指定 `capture_meta.json`。
- 给证据添加管理台注释，建议写入新表 `capture_annotations`，不直接写 `ctf_captures`。

建议新增：

```sql
CREATE TABLE IF NOT EXISTS capture_annotations (
    id INTEGER PRIMARY KEY,
    ctf_capture_id INTEGER NOT NULL REFERENCES ctf_captures(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'unreviewed',
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (status IN ('unreviewed', 'useful', 'noise', 'needs_login', 'blocked', 'error'))
);
```

#### Scheduler

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/scheduler/config` | 读取 `crawl_targets.json` |
| `GET` | `/api/scheduler/jobs` | 查看同步后的 `crawl_jobs` |
| `GET` | `/api/scheduler/reports` | 查看运行报告 |

调度边界：

- 首版只读 `crawl_targets.json`、`crawl_jobs` 和运行报告。
- 前端不保存 `crawl_targets.json`。
- 前端不执行 `crawl_runner.py --sync-only`。
- 前端不执行 dry-run。
- 前端不触发真实抓取。
- 配置修改、sync、dry-run 和真实抓取继续通过命令行完成。

#### Maintenance

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/api/maintenance/bootstrap` | 执行 bootstrap |
| `POST` | `/api/maintenance/backup-db` | 创建 DB 备份 |
| `GET` | `/api/audit-log` | 查看管理台审计日志 |

## 前端技术设计

### 运行方式

```bash
cd apps/admin_web
npm install
npm run dev
```

Vite 默认开发服务器端口是 `5173`。前端 dev server 通过 Vite proxy 转发 `/api` 到 `http://127.0.0.1:8787`。

### 前端依赖建议

基础：

- `react`
- `react-dom`
- `typescript`
- `vite`
- `@vitejs/plugin-react`

路由：

- `react-router`

数据请求：

- `@tanstack/react-query`

表格：

- `@tanstack/react-table`

表单：

- `react-hook-form`
- `zod`

UI：

- `lucide-react`
- 可选：Radix UI primitives
- 样式建议：CSS Modules 或 Tailwind。若引入 Tailwind，需要同时制定 tokens，避免随意堆 class。

### 视觉风格

管理台应是高密度、安静、可扫描的工作台：

- 左侧固定导航。
- 顶部只放数据库状态、写入状态、当前 DB 文件。
- 默认首页是记录工作台，筛选器和记录列表为主体。
- 详情区采用两栏或三栏：记录字段、图片/作者/互动、证据/JSON。
- 卡片圆角不超过 8px。
- 操作按钮用图标 + tooltip，危险操作使用明确文本和确认对话框。
- 主色不要单一紫蓝渐变；建议使用中性灰 + 少量状态色。

### 核心组件

| 组件 | 作用 |
|---|---|
| `AppShell` | 左侧导航、顶部状态栏 |
| `DataTable` | 分页、排序、列显隐、行选择 |
| `RecordWorkbench` | 默认首页，承载筛选器、记录列表和详情预览 |
| `RecordFilterBar` | 平台、城市、关键词、状态、日期、缺字段筛选 |
| `RecordTable` | 记录列表，支持分页、排序、列显隐和行选择 |
| `RecordDetailDrawer` | 选中记录的详情抽屉或详情页 |
| `RecordEditor` | 记录白名单字段编辑表单 |
| `RecordImagePreview` | 当前记录图片预览 |
| `AuthorPanel` | 当前记录作者信息 |
| `MetricsPanel` | 当前记录互动指标 |
| `EvidencePanel` | 当前记录关联证据和产物 |
| `ImageGrid` | 从记录进入的图片墙 |
| `JsonViewer` | 展示 JSON 字段 |
| `ArtifactViewer` | 展示 HTML/文本/截图路径 |
| `ConfirmDialog` | 删除和批量操作确认 |
| `AuditLogPanel` | 写操作审计 |

### 记录工作台布局

```text
顶部筛选条：
  平台 / 城市 / 关键词 / 状态 / 发布时间 / 抓取时间 / 缺字段 / 重置

主体左侧或中间：
  记录列表
    平台
    标题
    作者
    城市
    发布时间
    图片数
    状态

主体右侧或抽屉：
  选中记录预览
    图片缩略图
    作者摘要
    正文摘要
    互动指标
    证据状态
```

平台和城市筛选必须始终可见。城市输入不依赖平台选择，适合直接查询“某城市在所有平台下的记录”。

### 记录列表列

默认显示：

- 平台
- 标题
- 作者
- 发布时间
- 抓取时间
- 城市
- 关键词
- 图片数
- 点赞
- 评论
- 状态
- 操作

默认隐藏但可开启：

- `id`
- `platform_post_id`
- `canonical_url`
- `source_type`
- `author_followers_count`
- `post_favorites_count`
- `post_shares_count`
- `post_views_count`

### 记录详情布局

```text
顶部：平台 / 状态 / 标题 / 外链 / 保存按钮

左栏：可编辑字段
  基础信息
  作者信息
  互动指标
  发布时间和城市

中栏：正文与图片
  content_text
  web_post_images

右栏：证据与原始数据
  source_capture_id
  artifact_dir
  raw_sample_json
  metrics_json
  author_json
  关联 ctf_capture
```

详情页使用标签或分组切换：

- 图片：`web_post_images` 缩略图、原图预览、排序、URL、local_path 状态。
- 作者：展示名、平台 ID、主页、头像、简介、粉丝数、关注数、作品数、认证信息。
- 内容：标题、正文、城市、关键词、发布时间、来源 URL。
- 互动：点赞、收藏、评论、分享、转发、浏览量、`metrics_json`。
- 证据：关联 `ctf_captures`、截图、HTML、可见文本、证据图片。
- JSON：`raw_sample_json`、`author_json`、`metrics_json`。
- 审计：本记录相关 `admin_audit_log`。

### 图片预览规则

图片展示优先级：

1. `web_post_images.local_path` 对应本地文件。
2. `web_post_images.image_url` 远程 URL。
3. `author_avatar_url` 头像。
4. `ctf_capture_images.saved_path` 页面证据图片。

前端不直接使用本地文件路径。所有本地图片都通过后端：

```text
/api/images/{id}/preview
/api/captures/images/{id}/preview
```

图片组件状态：

- 加载中。
- 加载失败。
- 远程 URL 不可达。
- 本地文件缺失。
- 原图预览弹窗。
- 复制 URL。
- 打开外链。

## 数据写入边界

### 允许直接写

- 记录白名单字段，落库到 `web_posts`。
- 记录图片，落库到 `web_post_images`。
- `admin_audit_log`。
- `capture_annotations`。

### 间接写

- `crawl_jobs`：只由命令行 sync 更新，管理端不写。
- `source_platforms`：迁移前通过修改 `scripts/web_sites.py` 后执行 bootstrap；迁移后通过修改 `trippostcollect.platforms.registry` 后执行 bootstrap；UI 首版不支持。
- `ctf_captures`：通过重新导入 `capture_meta.json`。

### 不写

- `raw_sample_json`。
- `raw_meta_json`。
- `capture_meta_path`、`rendered_html_path` 等证据路径。
- `schema_migrations`。
- `cities`，除非后续单独做城市管理。

## CRUD 语义

### Create

首版 UI 不创建记录。记录只由现有抓取和入库链路创建：

- `mediacrawler_crawl.py`。
- `ctf_resource_crawl.py`。
- `import_ctf_captures.py`。
- 后续可能存在的受控导入脚本。

管理端的“新增”边界只允许出现在这些链路完成后：用户在记录工作台看到新记录，并对其做查看、修正、软删除或审计。

### Read

所有记录列表必须分页。默认 `page_size=50`，最大 `page_size=200`。

全文搜索首版使用 `LIKE`：

```sql
WHERE title LIKE ? OR content_text LIKE ? OR author_display_name LIKE ?
```

二期可考虑 FTS5。

### Update

更新时：

- 读取旧行。
- 校验字段白名单。
- 校验类型和范围。
- 写入审计。
- 更新 `updated_at=datetime('now')`。

### Delete

默认软删除：

```sql
UPDATE web_posts SET status='skipped', updated_at=datetime('now') WHERE id=?
```

首版不提供记录物理删除。删除动作只做软删除，并写审计。

图片物理删除只删除数据库行，不删除磁盘文件；磁盘清理另做维护工具。

## 校验规则

### `published_at`

- 可为空。
- 非空时必须能解析为 ISO datetime。
- UI 显示 Asia/Shanghai。
- 后端保存原字符串或规范化为 Asia/Shanghai ISO。
- 禁止自动用 `captured_at` 填充。

### 数值字段

- 允许为空。
- 非空时必须为整数且大于等于 0。
- 粉丝数为空不能当 0。

### JSON 字段

- `metrics_json`、`author_json` 必须是 JSON object。
- UI 提供格式化编辑器。
- 保存前后端重新 `json.loads` 校验。

### URL 字段

- 允许 `http://` 或 `https://`。
- `local_path` 不允许从前端直接提交项目外路径。

## 安全设计

首版是本机管理台，但仍按安全边界设计：

- 默认监听 `127.0.0.1`。
- 写入可通过 `TRIPPOST_ADMIN_WRITE_ENABLED=false` 禁用。
- 命令执行首版固定禁用，`TRIPPOST_ADMIN_ALLOW_COMMANDS=false`。
- 后端不提供 sync、dry-run 或真实抓取触发接口。
- 后端不接受前端传入任意 shell。
- 文件读取只允许项目根、`outputs/`、`data/runtime/`、图片保存目录。
- HTML 证据默认以纯文本或 iframe sandbox 展示，避免执行抓取页面脚本。
- 远程图片代理设置超时和最大响应大小。
- 所有危险操作二次确认。

## 后端服务分层

```text
apps/admin_api/app/
routers/
  records.py
  images.py
  captures.py
  scheduler.py
  overview.py
  maintenance.py

repositories/
  records_repo.py
  images_repo.py
  captures_repo.py
  scheduler_repo.py

services/
  image_service.py
  scheduler_service.py
  backup_service.py
  audit_service.py
  validation_service.py

src/trippostcollect/
  records/
  artifacts/
  scheduler/
  db/
```

Router 只处理 HTTP。管理端 service 处理 API 编排、权限、审计和备份。SQL repository、记录聚合、证据读取、调度配置读取等可复用能力放在 `src/trippostcollect/`，避免抓取脚本和管理端重复实现。

## 前端状态管理

使用 React Query 管理服务端状态：

- 列表查询按筛选条件生成 query key。
- 保存后 invalidate 对应列表和详情。
- 乐观更新只用于低风险字段；删除、批量操作不做乐观更新。

本地 UI 状态：

- 表格列显隐。
- 筛选器折叠状态。
- 当前选中行。
- 图片预览弹窗。
- JSON 编辑器展开状态。

## 测试策略

### 后端

使用临时 SQLite：

- bootstrap 后表存在。
- `GET /api/records` 支持平台、城市、关键词、状态和时间筛选。
- `GET /api/records/{id}/context` 返回图片、作者、互动、证据和 JSON 聚合数据。
- `PATCH /api/records/{id}` 只允许白名单字段。
- 软删除只改 `status`。
- 图片代理拒绝项目外路径。
- `ctf_captures` 只读。
- 调度 API 只读。
- 不存在从前端触发 sync、dry-run 或真实抓取的接口。

### 前端

- 表格渲染字段。
- 筛选器改变会重新请求。
- 编辑表单校验。
- 图片加载失败状态。
- 删除确认流程。
- JSON 字段非法时阻止保存。

### 集成

使用 `temp/admin_client_verify.sqlite`：

1. 创建临时库。
2. bootstrap。
3. 插入样本 `web_posts` 和 `web_post_images`。
4. 启动 FastAPI 测试客户端。
5. 验证记录筛选、记录详情上下文、CRUD 和审计日志。

## 开发阶段

### 阶段 0：文档、约束和结构准备

交付：

- 本文档。
- 明确技术栈和目录。
- 明确写入边界。
- 明确 `apps/`、`src/trippostcollect/`、`scripts/`、`data/`、`outputs/` 的职责。
- 明确旧脚本兼容策略。

验收：

- 不改变默认数据库和现有抓取命令。
- 文档中没有把管理端设计成表级浏览器。
- 文档中没有让 `apps/admin_api` 长期直接依赖 `scripts/*.py` 的方案。

### 阶段 1：共享包骨架

交付：

- `src/trippostcollect/__init__.py`。
- `src/trippostcollect/core/paths.py`。
- `src/trippostcollect/db/bootstrap.py`。
- `src/trippostcollect/platforms/registry.py`。
- `scripts/project_paths.py`、`scripts/db_bootstrap.py`、`scripts/web_sites.py` 的兼容 wrapper 或兼容导出。

验收：

- 现有 `scripts/crawl_runner.py --sync-only` 仍可运行。
- 临时 SQLite bootstrap 仍可创建完整 schema。
- 平台注册同步后仍是 8 个当前平台。
- 新代码可以从 `trippostcollect.core.paths` 读取默认 DB、config、outputs 和 runtime 路径。

### 阶段 2：后端基础

交付：

- FastAPI app。
- settings。
- SQLite 连接。
- overview API。
- records list/detail/context/update。
- record images list/preview。
- audit log。

验收：

- 能读取默认库。
- 能用临时库跑测试。
- 能按平台、城市名或平台 + 城市名筛选记录。
- 能返回单条记录下的图片、作者、互动、证据和 JSON 上下文。
- 写操作前有备份和审计。

### 阶段 3：前端基础

交付：

- Vite + React + TypeScript。
- AppShell。
- 记录工作台。
- 平台和城市筛选器。
- 记录列表。
- 记录详情。
- 图片预览。

验收：

- 浏览器可打开管理台。
- 能按平台、城市名或平台 + 城市名筛选记录。
- 能围绕选中记录查看图片、作者、互动、证据和 JSON。
- 能编辑记录白名单字段。
- 能预览本地和远程图片。

### 阶段 4：证据和调度

交付：

- 记录详情中的证据面板。
- 独立证据查询页。
- 截图/HTML/文本查看。
- 调度配置只读查看。
- 运行报告查看。

验收：

- `ctf_captures` 不被直接修改。
- 前端不能修改 `crawl_targets.json`。
- 前端不能触发 sync、dry-run 或真实抓取。
- 能查看当前 8 个同步任务和最近运行报告。

### 阶段 5：质量和打磨

交付：

- 批量编辑。
- 字段缺失修复工作流。
- 图片失效检查。
- 审计日志页。
- 文档补充。

验收：

- 常用管理动作不需要写 SQL。
- 危险操作都有确认和审计。
- 布局在 1280px 和移动窄屏下不重叠。

## 验收标准

首版完成标准：

- 默认首页是记录工作台，而不是表级数据浏览器。
- 可以通过平台、城市名或平台 + 城市名筛选记录。
- 可以围绕选中记录查看 `web_posts`、`web_post_images`、关联 `ctf_captures` 和 `crawl_run_reports` 上下文。
- 可以查看、更新、软删除记录白名单字段；记录创建只来自抓取和入库链路。
- 可以编辑、排序、删除记录图片行；图片新增默认来自入库链路。
- 可以预览记录图片和关联证据图片。
- 可以从记录详情查看截图、HTML、可见文本路径和 JSON 原始数据。
- 可以只读查看 `crawl_targets.json`、`crawl_jobs` 和运行报告。
- 前端不提供 sync、dry-run 或真实抓取按钮。
- 默认库中 `source_platforms=8`、`crawl_jobs=8` 的状态不会被管理台破坏。
- 所有写操作有备份和 `admin_audit_log`。
- `ctf_captures` 默认只读。
- 后端测试覆盖关键写入边界。

## 风险和决策

| 风险 | 影响 | 决策 |
|---|---|---|
| 直接改证据表破坏溯源 | 高 | `ctf_captures` 默认只读 |
| 前端误触发抓取或 sync | 高 | 首版调度只读，不提供命令执行接口 |
| 图片本地路径泄漏 | 中 | 后端代理，限制白名单路径 |
| SQLite 并发写冲突 | 中 | 单用户设计，短事务，写前备份 |
| 前端表格一次加载太多 | 中 | 强制分页 |
| JSON 字段被写坏 | 中 | 后端校验 JSON object |
| 删除误操作 | 高 | 默认软删除，物理删除严格限制 |

## 已确认边界和待定问题

1. 已确认：首版只在本机使用，默认监听 `127.0.0.1`。
2. 已确认：首版不手工新增记录，记录新增由现有抓取和入库链路完成。
3. 已确认：抓取任务不由前端使用，前端不触发 sync、dry-run 或真实抓取。
4. 待定：`web_posts` 是否需要新增 `review_status`、`review_note` 字段，还是先复用 `status` 和审计日志。

## 推荐首个实现任务

先实现结构准备和后端基础：

1. 创建 `src/trippostcollect/` 包骨架。
2. 抽取 `core.paths`、`db.bootstrap`、`platforms.registry`，保留 `scripts/` 兼容入口。
3. 创建 `apps/admin_api`。
4. 加入 settings、DB 连接、bootstrap。
5. 实现 `/api/meta`、`/api/overview/counts`、`/api/records`、`/api/records/{id}`。
6. 实现 `/api/records/{id}/context`，聚合图片、作者、互动、证据和 JSON。
7. 实现 `PATCH /api/records/{id}` 的白名单更新、备份和审计。
8. 用 `temp/admin_client_verify.sqlite` 做集成测试。

后端边界稳定后，再搭前端。这样可以避免 UI 先行导致 CRUD 规则散落在浏览器里。
