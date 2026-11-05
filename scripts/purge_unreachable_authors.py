#!/usr/bin/env python3
"""Delete still-masked posts whose authors are recorded as unreachable."""

from __future__ import annotations

from trippostcollect.application.author_identity_purge import main


if __name__ == "__main__":
    raise SystemExit(main())
