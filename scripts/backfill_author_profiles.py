#!/usr/bin/env python3
"""回填小红书认证两列，以及抖音作者主页、简介和认证。"""

from trippostcollect.application.author_profile_backfill import main as run_backfill
from trippostcollect.platforms.xhs.parser import creator_verified_fields


def main() -> int:
    return run_backfill(verified_fields=creator_verified_fields)


if __name__ == "__main__":
    raise SystemExit(main())
