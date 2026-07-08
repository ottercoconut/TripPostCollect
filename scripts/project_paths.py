"""Central project paths and directory creation helpers."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
CONFIG_ROOT = PROJECT_ROOT / "config"
DB_ROOT = PROJECT_ROOT / "db"
DATA_ROOT = PROJECT_ROOT / "data"
OUTPUTS_ROOT = PROJECT_ROOT / "outputs"
TEMP_ROOT = PROJECT_ROOT / "temp"
TOOLS_ROOT = PROJECT_ROOT / "tools"
RUNTIME_ROOT = DATA_ROOT / "runtime"

DEFAULT_DB = DATA_ROOT / "trippostcollect.sqlite"
DEFAULT_CONFIG = CONFIG_ROOT / "crawl_targets.json"

MEDIACRAWLER_DIR = TOOLS_ROOT / "MediaCrawler"
MEDIACRAWLER_RUNS_OUTPUT = OUTPUTS_ROOT / "mediacrawler_runs"
MEDIACRAWLER_LOGIN_OUTPUT = OUTPUTS_ROOT / "mediacrawler_login_warmup"
CTF_LOGIN_OUTPUT = OUTPUTS_ROOT / "ctf_login_warmup"
CTF_RESOURCE_OUTPUT = OUTPUTS_ROOT / "ctf_resource_crawls"

CRAWL_RUNNER_RUNTIME = RUNTIME_ROOT / "crawl_runner"
CRAWL_POLICY_STATE = RUNTIME_ROOT / "scrapling_throttle.json"
LOCK_DIR = RUNTIME_ROOT / "locks"

BROWSER_PROFILE_ROOT = DATA_ROOT / "browser_profiles"
BROWSER_STATE_ROOT = DATA_ROOT / "browser_state"
CTF_BROWSER_PROFILE_ROOT = DATA_ROOT / "browser_profiles_ctf"
STEALTH_BROWSER_PROFILE_ROOT = DATA_ROOT / "browser_profiles_stealth"

SOURCE_PLATFORMS_SCHEMA = DB_ROOT / "source_platforms.sql"
WEB_POSTS_SCHEMA = DB_ROOT / "web_posts.sql"
CTF_CAPTURES_SCHEMA = DB_ROOT / "ctf_captures.sql"
CRAWL_SCHEDULER_SCHEMA = DB_ROOT / "crawl_scheduler.sql"


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
