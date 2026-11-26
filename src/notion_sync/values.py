"""Converts plain Python values to Notion property JSON and back."""

import math
import reprlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings as django_settings
from django.utils import timezone
from django.utils.functional import Promise

from notion_sync.exceptions import NotionValidationError
from notion_sync.files import NotionFile

# Notion's limits per request.
TEXT_CHUNK = 2000
ARRAY_LIMIT = 100
SHORT_TEXT_LIMITS = {"url": 2000, "email": 200, "phone_number": 200}

READ_ONLY_TYPES = frozenset(
    {
        "formula",
        "rollup",
        "created_time",
        "last_edited_time",
        "created_by",
        "last_edited_by",
        "unique_id",
    }
)

SNAPSHOT_TAG = "__notion_sync__"


@dataclass(frozen=True)
class DateRange:
    """A Notion date with an end. Both ends are dates or both are datetimes."""

    start: date | datetime
    end: date | datetime


# --- Outbound -------------------------------------------------------------


def raise_invalid(prop, value, expected):
    raise NotionValidationError(
        f'Property "{prop.name}" ({prop.type}) expects {expected}; '
        f"got {reprlib.repr(value)} ({type(value).__name__}).",
        property=prop.name,
        notion_type=prop.type,
        value=value,
    )


def utf16_units(char):
    return 2 if ord(char) > 0xFFFF else 1


def split_text(text, limit=TEXT_CHUNK):
    """Split text into chunks of at most `limit` UTF-16 code units.

    Notion counts characters the way JavaScript does, so a character outside
    the Basic Multilingual Plane (most emoji) counts as two.
    """
    chunks: list[str] = []
    current: list[str] = []
    units = 0
    for char in text:
        size = utf16_units(char)
        if units + size > limit:
            chunks.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += size
    if current:
        chunks.append("".join(current))
    return chunks


def to_text(prop, value):
    if value is None:
        return []
    if isinstance(value, Promise):
        value = str(value)
    if not isinstance(value, str):
        raise_invalid(prop, value, "a string or None")
    chunks = split_text(value)
    if len(chunks) > ARRAY_LIMIT:
        raise_invalid(prop, value, f"at most {TEXT_CHUNK * ARRAY_LIMIT} characters")
    return [{"type": "text", "text": {"content": chunk}} for chunk in chunks]


def to_number(prop, value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise_invalid(prop, value, "a number or None")
    if isinstance(value, Decimal):
        value = int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise_invalid(prop, value, "a finite number")
    return value


def option_name(prop, value):
    if not isinstance(value, str) or not value:
        raise_invalid(prop, value, "a non-empty option name")
    if "," in value:
        raise_invalid(prop, value, "an option name without commas")
    return {"name": value}


def to_select(prop, value):
    if value is None:
        return None
    return option_name(prop, value)


def to_status(prop, value):
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise_invalid(prop, value, "a status name or None")
    return {"name": value}


def as_sequence(prop, value, expected):
    """A list from a list, tuple or set. A bare string is a common mistake."""
    if value is None:
        return []
    if isinstance(value, set | frozenset):
        value = sorted(value, key=str)
    if not isinstance(value, list | tuple):
        raise_invalid(prop, value, expected)
    if len(value) > ARRAY_LIMIT:
        raise_invalid(prop, value, f"at most {ARRAY_LIMIT} items")
    return list(value)


def to_multi_select(prop, value):
    items = as_sequence(prop, value, "a list of option names")
    return [option_name(prop, item) for item in items]


def date_string(prop, value, original):
    if isinstance(value, datetime):
        if timezone.is_naive(value) and django_settings.USE_TZ:
            value = timezone.make_aware(value)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise_invalid(prop, original, "a date, a datetime, a DateRange or None")


def to_date(prop, value):
    if value is None:
        return None
    if isinstance(value, DateRange):
        if isinstance(value.start, datetime) != isinstance(value.end, datetime):
            raise_invalid(prop, value, "a DateRange of two dates or two datetimes")
        return {
            "start": date_string(prop, value.start, value),
            "end": date_string(prop, value.end, value),
        }
    return {"start": date_string(prop, value, value), "end": None}


def to_checkbox(prop, value):
    if not isinstance(value, bool):
        raise_invalid(prop, value, "True or False")
    return value


def to_short_text(prop, value):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise_invalid(prop, value, "a string or None")
    limit = SHORT_TEXT_LIMITS[prop.type]
    if len(value) > limit:
        raise_invalid(prop, value, f"at most {limit} characters")
    return value


def id_list(prop, value, kind):
    items = as_sequence(prop, value, f"a list of {kind} IDs")
    for item in items:
        if not isinstance(item, str) or not item:
            raise_invalid(prop, value, f"a list of {kind} IDs")
    return items


def to_people(prop, value):
    return [{"object": "user", "id": item} for item in id_list(prop, value, "user")]


def to_relation(prop, value):
    return [{"id": item} for item in id_list(prop, value, "page")]


def to_files(prop, value):
    if isinstance(value, NotionFile | str):
        value = [value]
    items = as_sequence(prop, value, "a NotionFile, a URL, or a list of them")
    converted: list[NotionFile | dict] = []
    for item in items:
        if isinstance(item, NotionFile):
            converted.append(item)  # Uploaded after everything has validated.
        elif isinstance(item, str) and item.startswith(("https://", "http://")):
            name = item.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or item
            converted.append(
                {"type": "external", "name": name[:100], "external": {"url": item}}
            )
        else:
            raise_invalid(prop, value, "a NotionFile, a URL, or a list of them")
    return converted


OUTBOUND = {
    "title": to_text,
    "rich_text": to_text,
    "number": to_number,
    "select": to_select,
    "multi_select": to_multi_select,
    "status": to_status,
    "date": to_date,
    "checkbox": to_checkbox,
    "url": to_short_text,
    "email": to_short_text,
    "phone_number": to_short_text,
    "people": to_people,
    "relation": to_relation,
    "files": to_files,
}


def to_notion(schema, values, *, upload=None):
    """Convert `{property name: plain value}` to Notion property JSON.

    Every value is checked against the schema first. Files are uploaded, by
    calling `upload(notion_file)`, only once all values have passed.
    """
    converted = {}
    for name, value in values.items():
        prop = schema.get(name)
        if prop is None:
            raise NotionValidationError(
                f'The data source has no property named "{name}".',
                property=name,
                value=value,
            )
        converter = OUTBOUND.get(prop.type)
        if converter is None:
            reason = (
                "is read-only" if prop.type in READ_ONLY_TYPES else "is not supported"
            )
            raise NotionValidationError(
                f'Property "{name}" ({prop.type}) {reason} and cannot be written.',
                property=name,
                notion_type=prop.type,
                value=value,
            )
        converted[name] = {prop.type: converter(prop, value)}

    for body in converted.values():
        files = body.get("files")
        if not files:
            continue
        for index, item in enumerate(files):
            if isinstance(item, NotionFile):
                files[index] = {
                    "type": "file_upload",
                    "file_upload": {"id": upload(item)},
                    "name": item.name[:100],
                }
    return converted


# --- Inbound --------------------------------------------------------------


def parse_plain_text(items):
    return "".join(item.get("plain_text", "") for item in items or [])


def parse_date_string(value, time_zone=None):
    if len(value) == 10:
        return date.fromisoformat(value)
    parsed = datetime.fromisoformat(value)
    if time_zone and timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=ZoneInfo(time_zone))
    return parsed


def parse_date(value):
    if not value or not value.get("start"):
        return None
    start = parse_date_string(value["start"], value.get("time_zone"))
    if value.get("end"):
        return DateRange(start, parse_date_string(value["end"], value.get("time_zone")))
    return start


def parse_timestamp(value):
    return datetime.fromisoformat(value) if value else None


def parse_name(value):
    return value["name"] if value else None


def parse_user_id(value):
    return value.get("id") if value else None


def parse_files(items):
    files = []
    for item in items or []:
        kind = item.get("type")
        url = (item.get(kind) or {}).get("url") if kind else None
        files.append({"name": item.get("name"), "url": url})
    return files


def parse_unique_id(value):
    if not value or value.get("number") is None:
        return None
    if value.get("prefix"):
        return f"{value['prefix']}-{value['number']}"
    return value["number"]


def parse_formula(value):
    kind = value.get("type")
    if kind == "date":
        return parse_date(value.get("date"))
    return value.get(kind)


def parse_rollup(value):
    kind = value.get("type")
    if kind == "date":
        return parse_date(value.get("date"))
    if kind == "array":
        return [from_notion_value(item) for item in value.get("array") or []]
    return value.get(kind)


INBOUND = {
    "title": parse_plain_text,
    "rich_text": parse_plain_text,
    "select": parse_name,
    "status": parse_name,
    "multi_select": lambda items: [item["name"] for item in items or []],
    "date": parse_date,
    "people": lambda items: [item["id"] for item in items or []],
    "relation": lambda items: [item["id"] for item in items or []],
    "files": parse_files,
    "formula": parse_formula,
    "rollup": parse_rollup,
    "created_time": parse_timestamp,
    "last_edited_time": parse_timestamp,
    "created_by": parse_user_id,
    "last_edited_by": parse_user_id,
    "unique_id": parse_unique_id,
}

# Types whose JSON value is already the plain value.
PASS_THROUGH = frozenset({"number", "checkbox", "url", "email", "phone_number"})


def from_notion_value(prop):
    """Convert one property object from a page to a plain value."""
    kind = prop.get("type")
    raw = prop.get(kind)
    if kind in PASS_THROUGH:
        return raw
    parser = INBOUND.get(kind)
    if parser is None:
        return raw  # A type this package does not know: hand back Notion's JSON.
    return parser(raw)


def from_notion(properties):
    """Convert a page's `properties` object to `{name: plain value}`."""
    return {name: from_notion_value(prop) for name, prop in properties.items()}


# --- Outbox snapshots -----------------------------------------------------


def tagged(kind, value):
    return {SNAPSHOT_TAG: kind, "value": value}


def encode_value(name, value):
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Promise):
        return str(value)
    if isinstance(value, datetime):
        if timezone.is_naive(value) and django_settings.USE_TZ:
            value = timezone.make_aware(value)
        return tagged("datetime", value.isoformat())
    if isinstance(value, date):
        return tagged("date", value.isoformat())
    if isinstance(value, Decimal):
        return tagged("decimal", str(value))
    if isinstance(value, DateRange):
        bounds = [encode_value(name, value.start), encode_value(name, value.end)]
        return tagged("range", bounds)
    if isinstance(value, NotionFile):
        return tagged("file", value.to_snapshot())
    if isinstance(value, set | frozenset):
        value = sorted(value, key=str)
    if isinstance(value, list | tuple):
        return [encode_value(name, item) for item in value]
    raise NotionValidationError(
        f'Property "{name}": a {type(value).__name__} value cannot be queued; '
        f"got {reprlib.repr(value)}.",
        property=name,
        value=value,
    )


def decode_value(value):
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    if isinstance(value, dict) and SNAPSHOT_TAG in value:
        kind, inner = value[SNAPSHOT_TAG], value["value"]
        if kind == "datetime":
            return datetime.fromisoformat(inner)
        if kind == "date":
            return date.fromisoformat(inner)
        if kind == "decimal":
            return Decimal(inner)
        if kind == "range":
            return DateRange(decode_value(inner[0]), decode_value(inner[1]))
        if kind == "file":
            return NotionFile.from_snapshot(inner)
    return value


def encode_snapshot(values):
    """Turn plain values into JSON for an outbox row. Files become paths."""
    if not isinstance(values, dict):
        raise NotionValidationError("Properties must be a dict of name to value.")
    return {name: encode_value(name, value) for name, value in values.items()}


def decode_snapshot(data):
    return {name: decode_value(value) for name, value in data.items()}
