#!/usr/bin/env python3
"""Backfill raw author ids and nicknames for legacy masked Douyin, Weibo, and Zhihu rows."""

from __future__ import annotations

from trippostcollect.application.author_identity import main


if __name__ == "__main__":
    raise SystemExit(main())
