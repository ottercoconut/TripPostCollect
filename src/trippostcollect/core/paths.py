"""Central project paths and directory creation helpers."""

from __future__ import annotations

import os
from pathlib import Path


def _project_root() -> Path:
    """源码 checkout 默认以仓库为根；安装态（不在 src 布局内）必须显式给出工作根。

    判定：本文件上三级目录含 pyproject.toml，且其 src/trippostcollect/core/paths.py 就是本文件，即为源码
    checkout；否则视为安装态，不把 site-packages 的上级目录当作运行根。
    """
    configured = os.getenv("TRIPPOST_PROJECT_ROOT")
    if configured is not None:
        return Path(configured).expanduser().resolve()
    here = Path(__file__).resolve()
    checkout = here.parents[3]
    if (checkout / "pyproject.toml").is_file() and (checkout / "src/trippostcollect/core/paths.py").resolve() == here:
        return checkout
    raise RuntimeError("trippostcollect 以安装包运行时必须设置 TRIPPOST_PROJECT_ROOT 指向项目工作根")


PROJECT_ROOT = _project_root()
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
CONFIG_ROOT = PROJECT_ROOT / "config"
DB_ROOT = PROJECT_ROOT / "db"
DOCS_ROOT = PROJECT_ROOT / "docs"
DATA_ROOT = PROJECT_ROOT / "data"
OUTPUTS_ROOT = PROJECT_ROOT / "outputs"
TEMP_ROOT = PROJECT_ROOT / "temp"
RUNTIME_ROOT = DATA_ROOT / "runtime"
LOCAL_MEDIA_ROOT = DATA_ROOT / "media"

DEFAULT_DB = DATA_ROOT / "trippostcollect.sqlite"
DEFAULT_CONFIG = CONFIG_ROOT / "crawl_targets.json"
XHS_TARGET_CONFIG = CONFIG_ROOT / "xhs_targets.json"
XHS_POOL_CONFIG = CONFIG_ROOT / "xhs_pool.json"
FORMAL_CRAWL_CONTRACT = DOCS_ROOT / "formal-crawl-contract.md"

PLATFORM_PROFILE_CODES = {"bilibili": "bili", "weibo": "wb", "douyin": "dy", "zhihu": "zhihu", "xhs": "xhs"}
COOKIE_SNAPSHOT_FILENAME = "trippostcollect_cookie_snapshot.json"
# #127：通用平台持久 profile 在 warmup 登录与 crawl 标准模式启动时用同一视口，避免同一 profile
# 在 1280×900 与 1920×1080 之间来回切换。
PROFILE_VIEWPORT = {"width": 1280, "height": 900}
MEDIACRAWLER_RUNS_OUTPUT = OUTPUTS_ROOT / "mediacrawler_runs"
MEDIACRAWLER_LOGIN_OUTPUT = OUTPUTS_ROOT / "mediacrawler_login_warmup"
LOGIN_WARMUP_OUTPUT = OUTPUTS_ROOT / "login_warmup"
CTF_RESOURCE_OUTPUT = OUTPUTS_ROOT / "ctf_resource_crawls"
XHS_RUNS_OUTPUT = OUTPUTS_ROOT / "xhs_runs"
XHS_REPAIR_OUTPUT = OUTPUTS_ROOT / "xhs_post_repair"
POST_DETAIL_REPAIR_OUTPUT = OUTPUTS_ROOT / "post_detail_repair"
BILIBILI_REPAIR_OUTPUT = OUTPUTS_ROOT / "bilibili_article_repair"
DATABASE_MIGRATIONS_OUTPUT = OUTPUTS_ROOT / "database_migrations"

CRAWL_RUNNER_RUNTIME = RUNTIME_ROOT / "crawl_runner"
CRAWL_EXECUTION_STATE_ROOT = RUNTIME_ROOT / "crawl_execution_states"
CRAWL_POLICY_STATE = RUNTIME_ROOT / "scrapling_throttle.json"
LOCK_DIR = RUNTIME_ROOT / "locks"
FORMAL_MEDIA_PERSISTENCE_LOCK = LOCK_DIR / "formal_media_persistence.lock"
BROWSER_RUNTIME_HOME = RUNTIME_ROOT / "browser_home"
CHROME_CRASH_DUMPS = RUNTIME_ROOT / "chrome_crash_dumps"
PLATFORM_SESSIONS_ROOT = RUNTIME_ROOT / "platform_sessions"
DOUYIN_SLIDER_IMAGE_DIR = RUNTIME_ROOT / "douyin_slider_images"
XHS_RUNTIME_ROOT = RUNTIME_ROOT / "xhs"
XHS_EXECUTION_STATE_ROOT = XHS_RUNTIME_ROOT / "execution_states"
XHS_SESSION_ROOT = XHS_RUNTIME_ROOT / "sessions"
XHS_LOCK_ROOT = XHS_RUNTIME_ROOT / "locks"
XHS_LEGACY_ACCOUNT_ROOT = DATA_ROOT / "xhs_accounts"
XHS_REPAIR_RUNTIME_ROOT = XHS_RUNTIME_ROOT / "repairs"
XHS_RETRY_STATE_ROOT = XHS_RUNTIME_ROOT / "retry_states"
XHS_BATCH_CHECKPOINT_ROOT = XHS_RUNTIME_ROOT / "batch_checkpoints"
POST_DETAIL_REPAIR_RUNTIME_ROOT = RUNTIME_ROOT / "post_detail_repair"
POST_DETAIL_REPAIR_BACKUP_ROOT = DATA_ROOT / "backups" / "post_detail_repair"
BILIBILI_REPAIR_RUNTIME_ROOT = RUNTIME_ROOT / "bilibili_article_repair"
BILIBILI_REPAIR_BACKUP_ROOT = DATA_ROOT / "backups" / "bilibili_article_repair"
AUTHOR_AVATAR_REMOVAL_BACKUP_ROOT = DATA_ROOT / "backups" / "author_avatar_removal"
TOPIC_RELEVANCE_BACKUP_ROOT = DATA_ROOT / "backups" / "topic_relevance"

BROWSER_PROFILE_ROOT = DATA_ROOT / "browser_profiles"
BROWSER_STATE_ROOT = DATA_ROOT / "browser_state"
CTF_BROWSER_PROFILE_ROOT = DATA_ROOT / "browser_profiles_ctf"
STEALTH_BROWSER_PROFILE_ROOT = DATA_ROOT / "browser_profiles_stealth"
SOURCE_PLATFORMS_SCHEMA = DB_ROOT / "source_platforms.sql"
WEB_POSTS_SCHEMA = DB_ROOT / "web_posts.sql"
CTF_CAPTURES_SCHEMA = DB_ROOT / "ctf_captures.sql"
CRAWL_SCHEDULER_SCHEMA = DB_ROOT / "crawl_scheduler.sql"
XHS_CONTROL_SCHEMA = DB_ROOT / "xhs_control.sql"


def _persistent_session_platform(platform_key: str) -> str:
    """非小红书通用平台键；小红书每轮使用 XHS_SESSION_ROOT 下的空 session，不在此布局内。"""
    if platform_key not in PLATFORM_PROFILE_CODES:
        raise KeyError(platform_key)
    if platform_key == "xhs":
        raise ValueError("xhs uses run-scoped sessions under XHS_SESSION_ROOT, not platform_sessions")
    return platform_key


def platform_key_for_profile_code(code: str) -> str:
    """worker 配置的平台代号（wb/dy/zhihu/bili）转回项目平台键。"""
    for platform_key, profile_code in PLATFORM_PROFILE_CODES.items():
        if profile_code == code:
            return platform_key
    raise KeyError(code)


def platform_session_dir(platform_key: str) -> Path:
    return PLATFORM_SESSIONS_ROOT / _persistent_session_platform(platform_key)


def platform_profile_dir(platform_key: str) -> Path:
    """非小红书通用平台的持久 profile；只定义位置，不读取登录态。"""
    return platform_session_dir(platform_key) / "profile"


def platform_cdp_profile_dir(platform_key: str) -> Path:
    """CDP 模式且未声明共享 profile 时的独立 profile，与旧 `cdp_<code>_user_data_dir` 一一对应。"""
    return platform_session_dir(platform_key) / "cdp_profile"


def platform_cookie_snapshot_path(platform_key: str) -> Path:
    return platform_session_dir(platform_key) / COOKIE_SNAPSHOT_FILENAME


def ensure_dir(path: str | Path) -> Path:
    directory = Path(path).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def ensure_parent(path: str | Path) -> Path:
    target = Path(path).expanduser()
    ensure_dir(target.parent)
    return target


def output_dir(name: str) -> Path:
    return OUTPUTS_ROOT / name


def runtime_dir(name: str) -> Path:
    return RUNTIME_ROOT / name


def temp_path(name: str) -> Path:
    return TEMP_ROOT / name
