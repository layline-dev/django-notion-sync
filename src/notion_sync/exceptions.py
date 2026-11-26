"""Exceptions raised by the package.

No exception message ever contains a token.
"""

from typing import Any

__all__ = [
    "NotionAPIError",
    "NotionFatalError",
    "NotionNotFoundError",
    "NotionPermissionError",
    "NotionRateLimitError",
    "NotionRequestValidationError",
    "NotionRetryableError",
    "NotionSyncError",
    "NotionUncertainError",
    "NotionValidationError",
]

RETRYABLE = "retryable"
FATAL = "fatal"
UNCERTAIN = "uncertain"


class NotionSyncError(Exception):
    """Base class for every error the package raises."""


class NotionValidationError(NotionSyncError):
    """A value does not fit the Notion schema. Raised before any page request."""

    def __init__(
        self,
        message: str,
        *,
        property: str | None = None,
        notion_type: str | None = None,
        value: Any = None,
    ) -> None:
        super().__init__(message)
        self.property = property
        self.notion_type = notion_type
        self.value = value


class NotionAPIError(NotionSyncError):
    """A request to Notion failed.

    `outcome` says what the failure means for a retry:

    - "retryable": the request was not applied; sending it again is safe.
    - "fatal": sending the same request again will fail the same way.
    - "uncertain": the request may or may not have been applied. Repeating it
      is only safe if it is idempotent (an update is, a create is not).
    """

    outcome = FATAL

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after


class NotionRetryableError(NotionAPIError):
    """Notion did not apply the request. It can be sent again."""

    outcome = RETRYABLE


class NotionRateLimitError(NotionRetryableError):
    """Notion answered 429 or 529. `retry_after` holds the wait in seconds."""


class NotionUncertainError(NotionAPIError):
    """The request may have been applied: a timeout, dropped connection or 5xx."""

    outcome = UNCERTAIN


class NotionFatalError(NotionAPIError):
    """Notion rejected the request and will reject it again."""

    outcome = FATAL


class NotionRequestValidationError(NotionFatalError):
    """Notion answered 400 validation_error."""


class NotionPermissionError(NotionFatalError):
    """The token is invalid or lacks a capability (401 or 403)."""


class NotionNotFoundError(NotionFatalError):
    """The object does not exist or is not shared with the integration (404)."""
