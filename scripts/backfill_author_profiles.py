#!/usr/bin/env python3
"""回填小红书认证、抖音作者资料，或把知乎三处同为 0 的关注数改为 NULL。"""

from trippostcollect.application.author_profile_backfill import main as run_backfill
from trippostcollect.platforms.xhs.parser import creator_verified_fields


def main() -> int:
    return run_backfill(verified_fields=creator_verified_fields)


if __name__ == "__main__":
    raise SystemExit(main())
