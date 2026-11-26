"""The only module that imports the Notion SDK.

It wraps the handful of endpoints the package uses and turns every SDK or
transport error into one of the package's exceptions, classified as
retryable, fatal or uncertain.
"""

import logging
from typing import Any

import httpx
from notion_client import Client
from notion_client.errors import (
    HTTPResponseError,
    InvalidPathParameterError,
    RequestTimeoutError,
)

from notion_sync.conf import get_settings
from notion_sync.exceptions import (
    NotionAPIError,
    NotionFatalError,
    NotionNotFoundError,
    NotionPermissionError,
    NotionRateLimitError,
    NotionRequestValidationError,
    NotionRetryableError,
    NotionUncertainError,
)

# The SDK logs request failures here. Its messages carry status codes and
# Notion's error text, never the token.
sdk_logger = logging.getLogger("notion_sync.sdk")

# Rate-limit reasons that mean "stop", not "slow down".
BLOCKED_REASONS = {"public_api_request_blocked"}


def retry_after_seconds(error):
    """Seconds from a Retry-After header, or None."""
    headers = getattr(error, "headers", None)
    value = headers.get("retry-after") if headers is not None else None
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def ambiguous_error(message, *, write, **details):
    """A failure after the request may have reached Notion."""
    if write:
        return NotionUncertainError(message, **details)
    return NotionRetryableError(message, **details)


def translate_error(error, *, write):
    """Map an SDK or httpx error to a package exception.

    `write` is True for requests that change a page. Only those can be
    "uncertain"; an ambiguous failure on a read is simply retryable.
    """
    if isinstance(error, HTTPResponseError):
        status = error.status
        code = getattr(error.code, "value", error.code)
        details: dict[str, Any] = {"status": status, "code": code}
        message = f"Notion answered {status} ({code}): {error}"
        if status in (429, 529):
            reason = (getattr(error, "additional_data", None) or {}).get(
                "rate_limit_reason"
            )
            if reason in BLOCKED_REASONS:
                return NotionFatalError(message, **details)
            return NotionRateLimitError(
                message, retry_after=retry_after_seconds(error), **details
            )
        if status == 409:
            return NotionRetryableError(message, **details)
        if status in (401, 403):
            return NotionPermissionError(message, **details)
        if status == 404:
            return NotionNotFoundError(message, **details)
        if status == 400 and code == "validation_error":
            return NotionRequestValidationError(message, **details)
        if status >= 500:
            return ambiguous_error(message, write=write, **details)
        return NotionFatalError(message, **details)

    if isinstance(error, RequestTimeoutError):
        cause = error.__context__
        if isinstance(cause, httpx.ConnectTimeout | httpx.PoolTimeout):
            return NotionRetryableError("Timed out connecting to Notion.")
        return ambiguous_error("Timed out waiting for Notion to answer.", write=write)

    if isinstance(error, InvalidPathParameterError):
        return NotionFatalError(f"Invalid request path: {error}")

    if isinstance(error, httpx.ConnectError):
        return NotionRetryableError("Could not connect to Notion.")

    if isinstance(error, httpx.HTTPError):
        return ambiguous_error(
            f"Connection to Notion failed ({type(error).__name__}).", write=write
        )

    if isinstance(error, ValueError):
        # A 2xx answer whose body is not JSON.
        return ambiguous_error("Notion sent a response that is not JSON.", write=write)

    return None


class NotionClient:
    """Requests against one target. Use as a context manager, or call `close()`."""

    def __init__(self, target):
        settings = get_settings()
        self.target = target
        self._sdk = Client(
            auth=target.token,
            notion_version=settings.notion_version,
            timeout_ms=settings.timeout_seconds * 1000,
            retry=False,
            logger=sdk_logger,
            log_level=logging.WARNING,
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self):
        self._sdk.close()

    def _call(self, method, *args, write=False, **kwargs):
        try:
            return method(*args, **kwargs)
        except NotionAPIError:
            raise
        except Exception as error:
            translated = translate_error(error, write=write)
            if translated is None:
                raise
            # `from None` keeps the SDK error, and its request headers, out of
            # the traceback.
            raise translated from None

    def create_page(self, properties):
        return self._call(
            self._sdk.pages.create,
            write=True,
            parent={
                "type": "data_source_id",
                "data_source_id": self.target.data_source_id,
            },
            properties=properties,
        )

    def update_page(self, page_id, *, properties=None, in_trash=None):
        body = {}
        if properties is not None:
            body["properties"] = properties
        if in_trash is not None:
            body["in_trash"] = in_trash
        return self._call(self._sdk.pages.update, page_id, write=True, **body)

    def retrieve_page(self, page_id):
        return self._call(self._sdk.pages.retrieve, page_id)

    def retrieve_data_source(self):
        return self._call(self._sdk.data_sources.retrieve, self.target.data_source_id)

    def query_data_source(
        self, *, filter=None, sorts=None, page_size=None, start_cursor=None
    ):
        body = {}
        if filter is not None:
            body["filter"] = filter
        if sorts is not None:
            body["sorts"] = sorts
        if page_size is not None:
            body["page_size"] = page_size
        if start_cursor is not None:
            body["start_cursor"] = start_cursor
        return self._call(
            self._sdk.data_sources.query, self.target.data_source_id, **body
        )

    # A repeated upload only leaves an unattached file that Notion expires,
    # so upload requests are never "uncertain".

    def create_file_upload(self, *, filename, content_type=None, number_of_parts=None):
        body = {"filename": filename}
        if content_type:
            body["content_type"] = content_type
        if number_of_parts is None:
            body["mode"] = "single_part"
        else:
            body["mode"] = "multi_part"
            body["number_of_parts"] = number_of_parts
        return self._call(self._sdk.file_uploads.create, **body)

    def send_file_upload(
        self, upload_id, *, filename, content, content_type=None, part_number=None
    ):
        mime = content_type or "application/octet-stream"
        form: dict[str, Any] = {"file": (filename, content, mime)}
        if part_number is not None:
            form["part_number"] = str(part_number)
        return self._call(self._sdk.file_uploads.send, upload_id, **form)

    def complete_file_upload(self, upload_id):
        return self._call(self._sdk.file_uploads.complete, upload_id)
