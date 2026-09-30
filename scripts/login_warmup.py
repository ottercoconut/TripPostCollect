#!/usr/bin/env python3
"""登录命令入口，转发到包内编排。"""

from trippostcollect.application.warmup import (
    parse_args as parse_args,
    selected_targets as selected_targets,
    utc_iso as utc_iso,
    utc_stamp as utc_stamp,
    implementation_args as implementation_args,
    error_record as error_record,
    run_target as run_target,
    markdown_cell as markdown_cell,
    markdown_summary as markdown_summary,
    main_async as main_async,
    TARGETS as TARGETS,
    ALIASES as ALIASES,
    warmup_mediacrawler as warmup_mediacrawler,
)
from trippostcollect.application.warmup import login_main


def main() -> int:
    return login_main()


if __name__ == "__main__":
    raise SystemExit(main())
