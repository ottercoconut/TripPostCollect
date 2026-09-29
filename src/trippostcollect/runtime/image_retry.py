"""Bounded retry support for authoritative body-image byte requests."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any


IMAGE_DOWNLOAD_MAX_ATTEMPTS = 3
IMAGE_DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024
IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS = (1.0, 2.0)
RETRYABLE_IMAGE_ERROR_CODES = frozenset({"image_download_retryable"})
RETRYABLE_IMAGE_HTTP_STATUS_CODES = frozenset({408, 425})
RUNTIME_BLOCKING_IMAGE_ERROR_CODES = frozenset(
    {"image_auth_required", "image_rate_limited"}
)


class ImageDownloadFetchError(RuntimeError):
    """A classified image transport failure preserved across client boundaries."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
        http_status: int | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status
        self.attempts = attempts

    def with_attempts(self, attempts: int) -> "ImageDownloadFetchError":
        return ImageDownloadFetchError(
            str(self),
            code=self.code,
            retryable=self.retryable,
            http_status=self.http_status,
            attempts=attempts,
        )


def classified_http_image_error(status_code: int, message: str) -> ImageDownloadFetchError:
    if status_code in {401, 403}:
        return ImageDownloadFetchError(
            message,
            code="image_auth_required",
            retryable=False,
            http_status=status_code,
        )
    if status_code == 429:
        return ImageDownloadFetchError(
            message,
            code="image_rate_limited",
            retryable=False,
            http_status=status_code,
        )
    retryable = (
        status_code in RETRYABLE_IMAGE_HTTP_STATUS_CODES or status_code >= 500
    )
    return ImageDownloadFetchError(
        message,
        code=(
            "image_download_retryable" if retryable else "image_source_unavailable"
        ),
        retryable=retryable,
        http_status=status_code,
    )


def is_retryable_image_error(code: str | None) -> bool:
    """Return whether a recorded image failure is eligible for finite retry."""

    return str(code or "") in RETRYABLE_IMAGE_ERROR_CODES


def is_runtime_blocking_image_error(code: str | None) -> bool:
    """Return whether an image failure represents a run-level platform block."""

    return str(code or "") in RUNTIME_BLOCKING_IMAGE_ERROR_CODES


async def fetch_image_bytes_with_retry(
    fetcher: Callable[[], Awaitable[bytes | None]],
    *,
    logger: Any,
    label: str,
    max_attempts: int = IMAGE_DOWNLOAD_MAX_ATTEMPTS,
) -> tuple[bytes | None, int]:
    """Retry transient empty/timeout image responses with bounded backoff.

    Platform clients already log the underlying HTTP error. This helper adds a
    stable per-attempt retry trail while returning the total attempt count for
    the image manifest on both recovery and final failure.
    """

    if not 1 <= max_attempts <= IMAGE_DOWNLOAD_MAX_ATTEMPTS:
        raise ValueError(
            f"max_attempts must be between 1 and {IMAGE_DOWNLOAD_MAX_ATTEMPTS}"
        )

    for attempt in range(1, max_attempts + 1):
        timed_out = False
        fetch_error: ImageDownloadFetchError | None = None
        try:
            content = await fetcher()
        except ImageDownloadFetchError as exc:
            fetch_error = exc
            content = None
            if not exc.retryable:
                terminal = exc.with_attempts(attempt)
                logger.error(
                    f"[image_download_terminal] {label}, attempts={attempt}, "
                    f"error_code={terminal.code}, http_status={terminal.http_status}"
                )
                raise terminal from exc
        except TimeoutError:
            content = None
            timed_out = True

        if content:
            if attempt > 1:
                logger.info(
                    f"[image_download_retry_recovered] {label}, attempts={attempt}"
                )
            return content, attempt

        if attempt >= max_attempts:
            logger.error(
                f"[image_download_retry_exhausted] {label}, attempts={attempt}, "
                f"last_failure={fetch_error.code if fetch_error else 'timeout' if timed_out else 'empty_response'}"
            )
            if fetch_error is not None:
                raise fetch_error.with_attempts(attempt) from fetch_error
            return None, attempt

        delay = random.uniform(*IMAGE_DOWNLOAD_RETRY_DELAY_SECONDS) * (
            2 ** (attempt - 1)
        )
        logger.warning(
            f"[image_download_retry] {label}, attempt={attempt}/{max_attempts}, "
            f"failure={fetch_error.code if fetch_error else 'timeout' if timed_out else 'empty_response'}, "
            f"next_delay_seconds={delay:.3f}"
        )
        await asyncio.sleep(delay)

    raise AssertionError("image retry loop terminated unexpectedly")
