"""Checks on the Notion SDK that the package's design relies on.

respx fakes Notion at the HTTP layer, so the SDK itself runs for real. If an
SDK upgrade changes any of this, these tests fail before the package does.
"""

import httpx
import pytest
import respx
from notion_client import Client
from notion_client.errors import HTTPResponseError

NOTION_VERSION = "2025-09-03"
PAGE_ID = "11111111-2222-3333-4444-555555555555"
PAGE_URL = f"https://api.notion.com/v1/pages/{PAGE_ID}"


def make_client(**options):
    return Client(auth="test-token", notion_version=NOTION_VERSION, **options)


@respx.mock
def test_sdk_requests_are_faked_and_carry_the_pinned_version():
    route = respx.get(PAGE_URL).mock(
        return_value=httpx.Response(200, json={"object": "page", "id": PAGE_ID})
    )

    page = make_client(retry=False).pages.retrieve(PAGE_ID)

    assert page["id"] == PAGE_ID
    request = route.calls.last.request
    assert request.headers["Notion-Version"] == NOTION_VERSION
    assert request.headers["Authorization"] == "Bearer test-token"


@respx.mock
@pytest.mark.parametrize(
    ("status", "code"),
    [
        (429, "rate_limited"),
        (500, "internal_server_error"),
        (503, "service_unavailable"),
    ],
)
def test_sdk_sends_one_request_when_retries_are_disabled(status, code):
    route = respx.get(PAGE_URL).mock(
        return_value=httpx.Response(
            status,
            headers={"Retry-After": "0"},
            json={"object": "error", "status": status, "code": code, "message": "x"},
        )
    )

    with pytest.raises(HTTPResponseError):
        make_client(retry=False).pages.retrieve(PAGE_ID)

    assert route.call_count == 1


def test_sdk_exposes_the_endpoints_the_package_needs():
    client = make_client(retry=False)

    for endpoint, methods in {
        "pages": ["create", "update", "retrieve"],
        "data_sources": ["retrieve", "query", "update"],
        "file_uploads": ["create", "send", "complete"],
    }.items():
        for method in methods:
            assert callable(getattr(getattr(client, endpoint), method))
