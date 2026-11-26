"""Reads and validates the NOTION_SYNC setting."""

from dataclasses import dataclass, field

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured

NOTION_VERSION = "2025-09-03"

DEFAULTS = {
    "TARGETS": {},
    "NOTION_VERSION": NOTION_VERSION,
    "TIMEOUT_SECONDS": 30,
    "MAX_ATTEMPTS": 8,
    "RETENTION_DAYS": 7,
    "SCHEMA_CACHE_SECONDS": 300,
    "SENDING_TIMEOUT_SECONDS": 600,
    "DISPATCH": "command",
    "WEBHOOK_PROCESSING": "inline",
    "AUTO_SYNC": False,
    "ON_DELETE": "nothing",
    "ON_NOTION_TRASH": "nothing",
    "RECONCILE_LOOKBACK_HOURS": 24,
}

CHOICES = {
    "DISPATCH": ("command", "celery"),
    "WEBHOOK_PROCESSING": ("inline", "stored"),
    "ON_DELETE": ("nothing", "archive"),
    "ON_NOTION_TRASH": ("nothing", "unlink", "delete"),
}

POSITIVE_INTEGERS = ("TIMEOUT_SECONDS", "MAX_ATTEMPTS", "SENDING_TIMEOUT_SECONDS")
NON_NEGATIVE_INTEGERS = (
    "RETENTION_DAYS",
    "SCHEMA_CACHE_SECONDS",
    "RECONCILE_LOOKBACK_HOURS",
)

REQUIRED_TARGET_KEYS = ("TOKEN", "DATA_SOURCE_ID")
TARGET_KEYS = {
    "TOKEN",
    "DATA_SOURCE_ID",
    "EXTERNAL_ID_PROPERTY",
    "WEBHOOK_VERIFICATION_TOKEN",
}


@dataclass(frozen=True)
class Target:
    """One Notion data source and the token that can write to it."""

    name: str
    token: str = field(repr=False)
    data_source_id: str
    external_id_property: str | None = None
    webhook_verification_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Settings:
    targets: dict = field(repr=False)
    notion_version: str
    timeout_seconds: int
    max_attempts: int
    retention_days: int
    schema_cache_seconds: int
    sending_timeout_seconds: int
    dispatch: str
    webhook_processing: str
    auto_sync: bool
    on_delete: str
    on_notion_trash: str
    reconcile_lookback_hours: int


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def get_settings():
    """Return the validated NOTION_SYNC setting with defaults filled in.

    Targets are validated one at a time by `get_target`, so a target with a
    missing token does not break the others.
    """
    raw = getattr(django_settings, "NOTION_SYNC", {})
    if not isinstance(raw, dict):
        raise ImproperlyConfigured("NOTION_SYNC must be a dict.")
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise ImproperlyConfigured(
            f"NOTION_SYNC has unknown keys: {', '.join(unknown)}."
        )
    merged = {**DEFAULTS, **raw}

    if not isinstance(merged["TARGETS"], dict):
        raise ImproperlyConfigured("NOTION_SYNC['TARGETS'] must be a dict.")
    version = merged["NOTION_VERSION"]
    if not isinstance(version, str) or not version:
        raise ImproperlyConfigured("NOTION_SYNC['NOTION_VERSION'] must be a string.")
    for key, allowed in CHOICES.items():
        if merged[key] not in allowed:
            raise ImproperlyConfigured(
                f"NOTION_SYNC[{key!r}] must be one of {', '.join(allowed)}; "
                f"got {merged[key]!r}."
            )
    for key in POSITIVE_INTEGERS:
        if not is_int(merged[key]) or merged[key] < 1:
            raise ImproperlyConfigured(
                f"NOTION_SYNC[{key!r}] must be a positive integer."
            )
    for key in NON_NEGATIVE_INTEGERS:
        if not is_int(merged[key]) or merged[key] < 0:
            raise ImproperlyConfigured(
                f"NOTION_SYNC[{key!r}] must be zero or a positive integer."
            )
    if not isinstance(merged["AUTO_SYNC"], bool):
        raise ImproperlyConfigured("NOTION_SYNC['AUTO_SYNC'] must be True or False.")

    return Settings(**{key.lower(): value for key, value in merged.items()})


def get_target(target):
    """Return the `Target` for a name. A `Target` instance is returned as is."""
    if isinstance(target, Target):
        return target
    targets = get_settings().targets
    if target not in targets:
        raise ImproperlyConfigured(
            f"NOTION_SYNC['TARGETS'] has no target named {target!r}."
        )
    return parse_target(target, targets[target])


def parse_target(name, raw):
    """Validate one entry of NOTION_SYNC["TARGETS"] and return its `Target`."""
    where = f"NOTION_SYNC['TARGETS'][{name!r}]"
    if not isinstance(raw, dict):
        raise ImproperlyConfigured(f"{where} must be a dict.")
    unknown = sorted(set(raw) - TARGET_KEYS)
    if unknown:
        raise ImproperlyConfigured(f"{where} has unknown keys: {', '.join(unknown)}.")
    # Never echo a token value, even a malformed one.
    for key in REQUIRED_TARGET_KEYS:
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ImproperlyConfigured(f"{where} needs a non-empty {key}.")
    for key in ("EXTERNAL_ID_PROPERTY", "WEBHOOK_VERIFICATION_TOKEN"):
        value = raw.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise ImproperlyConfigured(
                f"{where}[{key!r}] must be a non-empty string or None."
            )
    return Target(
        name=name,
        token=raw["TOKEN"],
        data_source_id=raw["DATA_SOURCE_ID"],
        external_id_property=raw.get("EXTERNAL_ID_PROPERTY"),
        webhook_verification_token=raw.get("WEBHOOK_VERIFICATION_TOKEN"),
    )
