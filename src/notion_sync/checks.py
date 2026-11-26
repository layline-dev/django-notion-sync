"""Django system checks, registered by the app config. They never call Notion.

`manage.py notion_sync_check` covers what needs the network: the token, the
data source and the mapped properties.
"""

import importlib.util

from django.conf import settings as django_settings
from django.core.checks import CheckMessage, Error, Warning
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse

from notion_sync.conf import REQUIRED_TARGET_KEYS, Settings, get_settings, parse_target
from notion_sync.mapping import registered

# What each cache backend does to the webhook handshake token and seen event
# IDs, which every process serving or reading them has to share.
UNSHARED_CACHES = {
    "django.core.cache.backends.locmem.LocMemCache": "keeps entries in one process",
    "django.core.cache.backends.dummy.DummyCache": "stores nothing",
}


def valid_settings() -> Settings | None:
    """The NOTION_SYNC setting, or None if E001 reports it."""
    try:
        return get_settings()
    except ImproperlyConfigured:
        return None


def check_settings(**kwargs: object) -> list[CheckMessage]:
    try:
        settings = get_settings()
    except ImproperlyConfigured as error:
        return [
            Error(
                str(error),
                hint="See the Settings section of the django-notion-sync README.",
                id="notion_sync.E001",
            )
        ]
    messages: list[CheckMessage] = []
    for name, raw in settings.targets.items():
        messages.extend(check_target(name, raw))
    return messages


def check_target(name: str, raw: object) -> list[CheckMessage]:
    """E002 for a malformed target; W001 for each credential left unset.

    A credential usually comes from an environment variable that a build or
    CI job leaves unset, so it warns rather than stopping every command.
    """
    messages: list[CheckMessage] = []
    if isinstance(raw, dict):
        unset = [key for key in REQUIRED_TARGET_KEYS if is_unset(raw.get(key))]
        for key in unset:
            messages.append(
                Warning(
                    f"NOTION_SYNC['TARGETS'][{name!r}] has no {key}.",
                    hint="Calls to this target fail until it is set. If it is "
                    "read from an environment variable, set the variable.",
                    id="notion_sync.W001",
                )
            )
        # Stand-ins so the rest of the target is still validated.
        raw = {**raw, **dict.fromkeys(unset, "unset")}
    try:
        parse_target(name, raw)
    except ImproperlyConfigured as error:
        messages.append(Error(str(error), id="notion_sync.E002"))
    return messages


def is_unset(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def check_mappings(**kwargs: object) -> list[CheckMessage]:
    settings = valid_settings()
    if settings is None:
        return []
    return [
        Error(
            f"{type(mapping).__name__} uses target {mapping.target!r}, which is "
            "not in NOTION_SYNC['TARGETS'].",
            hint=f"Add {mapping.target!r} to NOTION_SYNC['TARGETS'], or fix the "
            "mapping's `target`.",
            obj=mapping.model,
            id="notion_sync.E003",
        )
        for mapping in registered()
        if mapping.target not in settings.targets
    ]


def check_dispatch(**kwargs: object) -> list[CheckMessage]:
    settings = valid_settings()
    if settings is None or settings.dispatch != "celery":
        return []
    if importlib.util.find_spec("celery") is not None:
        return []
    return [
        Error(
            "NOTION_SYNC['DISPATCH'] is 'celery', but Celery is not installed.",
            hint='Install the extra, `pip install "django-notion-sync[celery]"`, '
            "or set DISPATCH to 'command'.",
            id="notion_sync.E004",
        )
    ]


def check_webhook_cache(**kwargs: object) -> list[CheckMessage]:
    if not webhooks_enabled():
        return []
    backend = django_settings.CACHES.get("default", {}).get("BACKEND", "")
    problem = UNSHARED_CACHES.get(backend)
    if problem is None:
        return []
    short_name = backend.rsplit(".", 1)[-1]
    return [
        Warning(
            f"The webhook endpoint is installed, but the default cache "
            f"({short_name}) {problem}.",
            hint="The handshake token and the IDs of events already seen are "
            "kept in the default cache, so every web worker and management "
            "command must share it. Use Redis, Memcached or the database "
            "cache. A single-process development server can ignore this.",
            id="notion_sync.W002",
        )
    ]


def webhooks_enabled() -> bool:
    """Whether the project's URLconf includes `notion_sync.urls`."""
    try:
        reverse("notion_sync:webhook")
    except Exception:  # A broken URLconf is reported by Django's own checks.
        return False
    return True


ALL_CHECKS = (check_settings, check_mappings, check_dispatch, check_webhook_cache)
