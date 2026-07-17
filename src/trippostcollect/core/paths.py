"""Central project paths and directory creation helpers."""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(os.getenv("TRIPPOST_PROJECT_ROOT", str(Path(__file__).resolve().parents[3]))).expanduser().resolve()
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
CONFIG_ROOT = PROJECT_ROOT / "config"
DB_ROOT = PROJECT_ROOT / "db"
DOCS_ROOT = PROJECT_ROOT / "docs"
DATA_ROOT = PROJECT_ROOT / "data"
OUTPUTS_ROOT = PROJECT_ROOT / "outputs"
TEMP_ROOT = PROJECT_ROOT / "temp"
TOOLS_ROOT = PROJECT_ROOT / "tools"
RUNTIME_ROOT = DATA_ROOT / "runtime"

DEFAULT_DB = DATA_ROOT / "trippostcollect.sqlite"
DEFAULT_CONFIG = CONFIG_ROOT / "crawl_targets.json"
XHS_TARGET_CONFIG = CONFIG_ROOT / "xhs_targets.json"
XHS_POOL_CONFIG = CONFIG_ROOT / "xhs_pool.json"
FORMAL_CRAWL_CONTRACT = DOCS_ROOT / "formal-crawl-contract.md"

MEDIACRAWLER_DIR = TOOLS_ROOT / "MediaCrawler"
MEDIACRAWLER_RUNS_OUTPUT = OUTPUTS_ROOT / "mediacrawler_runs"
MEDIACRAWLER_LOGIN_OUTPUT = OUTPUTS_ROOT / "mediacrawler_login_warmup"
LOGIN_WARMUP_OUTPUT = OUTPUTS_ROOT / "login_warmup"
CTF_RESOURCE_OUTPUT = OUTPUTS_ROOT / "ctf_resource_crawls"
XHS_RUNS_OUTPUT = OUTPUTS_ROOT / "xhs_runs"
XHS_LOGIN_OUTPUT = OUTPUTS_ROOT / "xhs_login"

CRAWL_RUNNER_RUNTIME = RUNTIME_ROOT / "crawl_runner"
CRAWL_EXECUTION_STATE_ROOT = RUNTIME_ROOT / "crawl_execution_states"
CRAWL_POLICY_STATE = RUNTIME_ROOT / "scrapling_throttle.json"
LOCK_DIR = RUNTIME_ROOT / "locks"
UV_CACHE_ROOT = RUNTIME_ROOT / "uv_cache"
BROWSER_RUNTIME_HOME = RUNTIME_ROOT / "browser_home"
CHROME_CRASH_DUMPS = RUNTIME_ROOT / "chrome_crash_dumps"
XHS_RUNTIME_ROOT = RUNTIME_ROOT / "xhs"
XHS_EXECUTION_STATE_ROOT = XHS_RUNTIME_ROOT / "execution_states"

BROWSER_PROFILE_ROOT = DATA_ROOT / "browser_profiles"
BROWSER_STATE_ROOT = DATA_ROOT / "browser_state"
CTF_BROWSER_PROFILE_ROOT = DATA_ROOT / "browser_profiles_ctf"
STEALTH_BROWSER_PROFILE_ROOT = DATA_ROOT / "browser_profiles_stealth"
XHS_ACCOUNT_ROOT = DATA_ROOT / "xhs_accounts"

SOURCE_PLATFORMS_SCHEMA = DB_ROOT / "source_platforms.sql"
WEB_POSTS_SCHEMA = DB_ROOT / "web_posts.sql"
CTF_CAPTURES_SCHEMA = DB_ROOT / "ctf_captures.sql"
CRAWL_SCHEDULER_SCHEMA = DB_ROOT / "crawl_scheduler.sql"
XHS_CONTROL_SCHEMA = DB_ROOT / "xhs_control.sql"


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
