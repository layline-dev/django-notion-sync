import logging

import httpx
import pytest

from notion_sync.client import NotionClient
from notion_sync.conf import get_target
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
from tests.fake_notion import error_response
from tests.settings import INBOX_TOKEN


@pytest.fixture
def client():
    with NotionClient(get_target("inbox")) as client:
        yield client


def test_requests_carry_the_token_and_the_pinned_version(notion, client):
    client.retrieve_data_source()

    request = notion.requests[-1]
    assert request.headers["Authorization"] == f"Bearer {INBOX_TOKEN}"
    assert request.headers["Notion-Version"] == "2025-09-03"


def test_notion_version_comes_from_settings(notion, settings):
    settings.NOTION_SYNC = {**settings.NOTION_SYNC, "NOTION_VERSION": "2026-03-11"}

    with NotionClient(get_target("inbox")) as client:
        client.retrieve_data_source()

    assert notion.requests[-1].headers["Notion-Version"] == "2026-03-11"


def test_create_page_sends_the_data_source_parent(notion, client):
    client.create_page({"Name": {"title": []}})

    assert notion.last_json("POST", "/pages")["parent"] == {
        "type": "data_source_id",
        "data_source_id": "ds-inbox",
    }


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (400, "validation_error", NotionRequestValidationError),
        (400, "invalid_json", NotionFatalError),
        (401, "unauthorized", NotionPermissionError),
        (403, "restricted_resource", NotionPermissionError),
        (404, "object_not_found", NotionNotFoundError),
        (409, "conflict_error", NotionRetryableError),
        (429, "rate_limited", NotionRateLimitError),
        (529, "service_overload", NotionRateLimitError),
        (500, "internal_server_error", NotionUncertainError),
        (502, "bad_gateway", NotionUncertainError),
        (503, "service_unavailable", NotionUncertainError),
        (504, "gateway_timeout", NotionUncertainError),
    ],
)
def test_error_responses_on_a_write_are_classified(
    notion, client, status, code, expected
):
    notion.fail_next("POST", "/pages", error_response(status, code))

    with pytest.raises(NotionAPIError) as raised:
        client.create_page({})

    assert type(raised.value) is expected
    assert raised.value.status == status
    assert notion.count("POST", "/pages") == 1  # The SDK does not retry.


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (httpx.ConnectError("refused"), NotionRetryableError),
        (httpx.ConnectTimeout("slow"), NotionRetryableError),
        (httpx.PoolTimeout("busy"), NotionRetryableError),
        (httpx.ReadTimeout("slow"), NotionUncertainError),
        (httpx.WriteTimeout("slow"), NotionUncertainError),
        (httpx.ReadError("reset"), NotionUncertainError),
        (httpx.RemoteProtocolError("closed"), NotionUncertainError),
        (httpx.Response(200, text="<html>not json</html>"), NotionUncertainError),
    ],
)
def test_transport_failures_on_a_write_are_classified(
    notion, client, failure, expected
):
    notion.fail_next("POST", "/pages", failure)

    with pytest.raises(NotionAPIError) as raised:
        client.create_page({})

    assert type(raised.value) is expected


@pytest.mark.parametrize(
    "failure",
    [
        error_response(503, "service_unavailable"),
        httpx.ReadTimeout("slow"),
        httpx.ReadError("reset"),
    ],
)
def test_ambiguous_failures_on_a_read_are_simply_retryable(notion, client, failure):
    notion.fail_next("GET", "/data_sources/", failure)

    with pytest.raises(NotionAPIError) as raised:
        client.retrieve_data_source()

    assert type(raised.value) is NotionRetryableError


def test_rate_limit_carries_retry_after(notion, client):
    notion.fail_next(
        "POST",
        "/pages",
        error_response(429, "rate_limited", headers={"Retry-After": "7"}),
    )

    with pytest.raises(NotionRateLimitError) as raised:
        client.create_page({})

    assert raised.value.retry_after == 7


def test_a_blocked_integration_is_not_retried(notion, client):
    response = httpx.Response(
        429,
        json={
            "object": "error",
            "status": 429,
            "code": "rate_limited",
            "message": "Blocked.",
            "additional_data": {"rate_limit_reason": "public_api_request_blocked"},
        },
    )
    notion.fail_next("POST", "/pages", response)

    with pytest.raises(NotionAPIError) as raised:
        client.create_page({})

    assert type(raised.value) is NotionFatalError


def test_the_token_never_reaches_errors_or_logs(notion, client, caplog):
    caplog.set_level(logging.DEBUG)
    failures = [
        error_response(401, "unauthorized", "API token is invalid."),
        error_response(500, "internal_server_error"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("refused"),
    ]
    for failure in failures:
        notion.fail_next("POST", "/pages", failure)
        with pytest.raises(NotionAPIError) as raised:
            client.create_page({})
        error = raised.value
        assert INBOX_TOKEN not in str(error)
        assert INBOX_TOKEN not in repr(error)
        # Nothing chained to the error may carry the request and its headers.
        assert error.__cause__ is None
        assert error.__suppress_context__ is True

    assert INBOX_TOKEN not in caplog.text
