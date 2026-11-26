import os
from collections import Counter
from io import StringIO
from pathlib import Path

import pytest
import respx
from django.core.cache import cache
from django.core.files.storage import storages
from django.core.management import call_command

import notion_sync
from notion_sync import NotionMapping, Out, outbox
from notion_sync.mapping import get_mapping
from notion_sync.signals import page_synced, sync_failed
from notion_sync.tasks import drain_outbox
from tests import celery_app, fake_notion

LIVE_TOKEN_ENV = "NOTION_LIVE_TOKEN"


def load_env_file(path):
    """Read KEY=value lines from a .env file into the environment.

    Only for local runs of the live suite. Variables that are already set win,
    and empty values are ignored, so a blank template changes nothing.
    """
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if key and value and not os.environ.get(key):
            os.environ[key] = value


load_env_file(Path(__file__).resolve().parent.parent / ".env")


def pytest_collection_modifyitems(config, items):
    """Skip tests marked `live` unless a Notion token is in the environment."""
    if os.environ.get(LIVE_TOKEN_ENV):
        return
    skip_live = pytest.mark.skip(reason=f"{LIVE_TOKEN_ENV} is not set")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def notion():
    """An in-memory Notion. Any request it does not know fails the test."""
    router, fake = fake_notion.start()
    try:
        yield fake
    finally:
        router.stop()


@pytest.fixture
def no_network():
    """Fail the test if anything is sent over HTTP."""
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


@pytest.fixture
def storage():
    """The default (in-memory) storage, emptied after the test."""
    default = storages["default"]
    yield default
    for name in default.listdir("")[1]:
        default.delete(name)


@pytest.fixture
def captured_signals():
    """Record every page_synced and sync_failed signal as (name, kwargs)."""
    received = []

    def on_synced(sender, **kwargs):
        received.append(("page_synced", kwargs))

    def on_failed(sender, **kwargs):
        received.append(("sync_failed", kwargs))

    page_synced.connect(on_synced)
    sync_failed.connect(on_failed)
    yield received
    page_synced.disconnect(on_synced)
    sync_failed.disconnect(on_failed)


@pytest.fixture(params=["command", "celery"])
def drain(request, monkeypatch):
    """Run the outbox through the drain command or the Celery task.

    Both paths must pass the same tests. Returns the counts by status, as
    `outbox.drain` does.
    """
    assert celery_app.app.conf.task_always_eager

    if request.param == "celery":

        def run(limit=None):
            return Counter(drain_outbox.apply(kwargs={"limit": limit}).get())

        return run

    real = outbox.drain
    seen = []

    def spy(**kwargs):
        seen.append(real(**kwargs))
        return seen[-1]

    monkeypatch.setattr(outbox, "drain", spy)

    def run(limit=None):
        args = ["--limit", str(limit)] if limit is not None else []
        call_command("notion_sync_drain", *args, stdout=StringIO())
        return seen[-1]

    return run


@pytest.fixture
def register():
    """Register mappings for the test; unregister them afterwards."""
    models = []

    def do(model, **attrs):
        attrs.setdefault("target", "inbox")
        attrs.setdefault("fields", {"Name": Out("name")})
        mapping_class = type("TempMapping", (NotionMapping,), attrs)
        notion_sync.register(model)(mapping_class)
        models.append(model)
        return get_mapping(model)

    yield do
    for model in models:
        notion_sync.unregister(model)
