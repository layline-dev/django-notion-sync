import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from notion_sync.exceptions import NotionValidationError
from notion_sync.files import NotionFile
from notion_sync.schema import Property, Schema
from notion_sync.values import (
    DateRange,
    decode_snapshot,
    encode_snapshot,
    from_notion,
    split_text,
    to_notion,
)
from tests.fake_notion import SCHEMA

schema = Schema({name: Property(name, "id", kind) for name, kind in SCHEMA.items()})


def text(content):
    return [{"type": "text", "text": {"content": content}}]


def convert(name, value):
    return to_notion(schema, {name: value})[name]


# --- Outbound ---


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("Name", "Hello", {"title": text("Hello")}),
        ("Name", None, {"title": []}),
        ("Text", "Body", {"rich_text": text("Body")}),
        ("Text", "", {"rich_text": []}),
        ("Number", 3, {"number": 3}),
        ("Number", 2.5, {"number": 2.5}),
        ("Number", Decimal("4.25"), {"number": 4.25}),
        ("Number", Decimal("7"), {"number": 7}),
        ("Number", None, {"number": None}),
        ("Select", "High", {"select": {"name": "High"}}),
        ("Select", None, {"select": None}),
        ("Tags", ["bug", "web"], {"multi_select": [{"name": "bug"}, {"name": "web"}]}),
        ("Tags", ("bug",), {"multi_select": [{"name": "bug"}]}),
        ("Tags", {"b", "a"}, {"multi_select": [{"name": "a"}, {"name": "b"}]}),
        ("Tags", [], {"multi_select": []}),
        ("Tags", None, {"multi_select": []}),
        ("Status", "Done", {"status": {"name": "Done"}}),
        ("Status", None, {"status": None}),
        ("Due", date(2026, 12, 1), {"date": {"start": "2026-12-01", "end": None}}),
        ("Due", None, {"date": None}),
        ("Done", True, {"checkbox": True}),
        ("Done", False, {"checkbox": False}),
        ("Link", "https://example.com", {"url": "https://example.com"}),
        ("Link", "", {"url": None}),
        ("Link", None, {"url": None}),
        ("Email", "a@example.com", {"email": "a@example.com"}),
        ("Phone", "+1 555 0100", {"phone_number": "+1 555 0100"}),
        ("Owners", ["u1"], {"people": [{"object": "user", "id": "u1"}]}),
        ("Owners", [], {"people": []}),
        ("Related", ["p1", "p2"], {"relation": [{"id": "p1"}, {"id": "p2"}]}),
        ("Related", None, {"relation": []}),
        (
            "Files",
            "https://example.com/a/report.pdf?x=1",
            {
                "files": [
                    {
                        "type": "external",
                        "name": "report.pdf",
                        "external": {"url": "https://example.com/a/report.pdf?x=1"},
                    }
                ]
            },
        ),
        ("Files", [], {"files": []}),
    ],
)
def test_outbound_values(name, value, expected):
    assert convert(name, value) == expected


def test_outbound_datetimes():
    aware = datetime(2026, 12, 1, 9, 30, tzinfo=timezone(timedelta(hours=2)))
    assert convert("Due", aware)["date"]["start"] == "2026-12-01T09:30:00+02:00"

    # A naive datetime is read in Django's current time zone (UTC in tests).
    naive = datetime(2026, 12, 1, 9, 30)
    assert convert("Due", naive)["date"]["start"] == "2026-12-01T09:30:00+00:00"


def test_outbound_date_range():
    value = DateRange(date(2026, 12, 1), date(2026, 12, 3))

    assert convert("Due", value) == {
        "date": {"start": "2026-12-01", "end": "2026-12-03"}
    }


def test_long_text_is_split_into_2000_character_chunks():
    body = "a" * 4500

    chunks = convert("Text", body)["rich_text"]

    assert [len(chunk["text"]["content"]) for chunk in chunks] == [2000, 2000, 500]
    assert "".join(chunk["text"]["content"] for chunk in chunks) == body


def test_text_is_split_by_utf16_length_without_breaking_characters():
    # An emoji counts as two characters in Notion's limit.
    body = "a" + "😀" * 1500

    chunks = split_text(body)

    assert "".join(chunks) == body
    for chunk in chunks:
        assert len(chunk.encode("utf-16-le")) // 2 <= 2000
    assert len(chunks) == 2


def test_text_beyond_100_chunks_is_rejected():
    with pytest.raises(NotionValidationError, match="at most 200000 characters"):
        convert("Text", "a" * 200_001)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("Name", 5),
        ("Text", ["a"]),
        ("Number", "5"),
        ("Number", True),
        ("Number", float("nan")),
        ("Select", 3),
        ("Select", "a,b"),
        ("Select", ""),
        ("Tags", "bug"),
        ("Tags", ["ok", "a,b"]),
        ("Tags", [1]),
        ("Tags", [str(n) for n in range(101)]),
        ("Status", ["Done"]),
        ("Due", "2026-12-01"),
        ("Due", DateRange(date(2026, 12, 1), datetime(2026, 12, 2, tzinfo=UTC))),
        ("Done", 1),
        ("Done", None),
        ("Link", 42),
        ("Link", "https://example.com/" + "a" * 2000),
        ("Email", "a" * 201),
        ("Owners", "u1"),
        ("Owners", [None]),
        ("Related", "p1"),
        ("Files", 42),
        ("Files", ["not-a-url"]),
    ],
)
def test_mismatched_values_are_rejected(name, value):
    with pytest.raises(NotionValidationError) as raised:
        convert(name, value)

    assert raised.value.property == name
    assert raised.value.notion_type == SCHEMA[name]


def test_validation_error_names_the_property_type_and_value():
    with pytest.raises(NotionValidationError) as raised:
        convert("Number", "five")

    message = str(raised.value)
    assert '"Number"' in message
    assert "(number)" in message
    assert "'five'" in message
    assert "str" in message


@pytest.mark.parametrize("name", ["Score", "Ticket", "Created"])
def test_read_only_properties_cannot_be_written(name):
    with pytest.raises(NotionValidationError, match="read-only"):
        convert(name, "x")


def test_unsupported_property_types_cannot_be_written():
    odd = Schema({"Push": Property("Push", "id", "button")})

    with pytest.raises(NotionValidationError, match="not supported"):
        to_notion(odd, {"Push": "x"})


def test_unknown_property_is_rejected():
    with pytest.raises(NotionValidationError, match='no property named "Nope"'):
        convert("Nope", "x")


def test_files_are_uploaded_only_after_everything_validates(storage):
    storage.save("shot.png", __import__("io").BytesIO(b"png"))
    uploaded = []

    def upload(notion_file):
        uploaded.append(notion_file.name)
        return "upload-1"

    with pytest.raises(NotionValidationError):
        to_notion(
            schema, {"Files": NotionFile("shot.png"), "Number": "bad"}, upload=upload
        )
    assert uploaded == []

    result = to_notion(schema, {"Files": NotionFile("shot.png")}, upload=upload)
    assert uploaded == ["shot.png"]
    assert result["Files"] == {
        "files": [
            {
                "type": "file_upload",
                "file_upload": {"id": "upload-1"},
                "name": "shot.png",
            }
        ]
    }


# --- Inbound ---


def rich(content):
    return [{"type": "text", "text": {"content": content}, "plain_text": content}]


def test_inbound_values():
    properties = {
        "Name": {"type": "title", "title": rich("Hello ") + rich("world")},
        "Text": {"type": "rich_text", "rich_text": []},
        "Number": {"type": "number", "number": 3.5},
        "Select": {"type": "select", "select": {"id": "x", "name": "High"}},
        "Empty select": {"type": "select", "select": None},
        "Tags": {
            "type": "multi_select",
            "multi_select": [{"name": "a"}, {"name": "b"}],
        },
        "Status": {"type": "status", "status": {"name": "Done"}},
        "Due": {"type": "date", "date": {"start": "2026-12-01", "end": None}},
        "No date": {"type": "date", "date": None},
        "Done": {"type": "checkbox", "checkbox": True},
        "Link": {"type": "url", "url": "https://example.com"},
        "Email": {"type": "email", "email": None},
        "Phone": {"type": "phone_number", "phone_number": "+1"},
        "Owners": {"type": "people", "people": [{"object": "user", "id": "u1"}]},
        "Related": {"type": "relation", "relation": [{"id": "p1"}]},
        "Files": {
            "type": "files",
            "files": [
                {"name": "a.png", "type": "file", "file": {"url": "https://s3/a.png"}},
                {
                    "name": "b.pdf",
                    "type": "external",
                    "external": {"url": "https://example.com/b.pdf"},
                },
            ],
        },
        "Score": {"type": "formula", "formula": {"type": "number", "number": 16}},
        "When": {
            "type": "formula",
            "formula": {"type": "date", "date": {"start": "2026-09-15", "end": None}},
        },
        "Total": {
            "type": "rollup",
            "rollup": {"type": "number", "number": 8, "function": "sum"},
        },
        "Names": {
            "type": "rollup",
            "rollup": {
                "type": "array",
                "array": [{"type": "title", "title": rich("One")}],
                "function": "show_original",
            },
        },
        "Ticket": {"type": "unique_id", "unique_id": {"number": 42, "prefix": "TASK"}},
        "Plain ticket": {
            "type": "unique_id",
            "unique_id": {"number": 7, "prefix": None},
        },
        "Created": {"type": "created_time", "created_time": "2026-09-01T09:00:00.000Z"},
        "Edited": {
            "type": "last_edited_time",
            "last_edited_time": "2026-09-04T14:30:00.000Z",
        },
        "Creator": {"type": "created_by", "created_by": {"object": "user", "id": "u9"}},
        "Editor": {
            "type": "last_edited_by",
            "last_edited_by": {"object": "user", "id": "u8"},
        },
        "Push": {"type": "button", "button": {}},
    }

    assert from_notion(properties) == {
        "Name": "Hello world",
        "Text": "",
        "Number": 3.5,
        "Select": "High",
        "Empty select": None,
        "Tags": ["a", "b"],
        "Status": "Done",
        "Due": date(2026, 12, 1),
        "No date": None,
        "Done": True,
        "Link": "https://example.com",
        "Email": None,
        "Phone": "+1",
        "Owners": ["u1"],
        "Related": ["p1"],
        "Files": [
            {"name": "a.png", "url": "https://s3/a.png"},
            {"name": "b.pdf", "url": "https://example.com/b.pdf"},
        ],
        "Score": 16,
        "When": date(2026, 9, 15),
        "Total": 8,
        "Names": ["One"],
        "Ticket": "TASK-42",
        "Plain ticket": 7,
        "Created": datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
        "Edited": datetime(2026, 9, 4, 14, 30, tzinfo=UTC),
        "Creator": "u9",
        "Editor": "u8",
        "Push": {},
    }


def test_inbound_datetimes_and_ranges():
    def parse(value):
        return from_notion({"Due": {"type": "date", "date": value}})["Due"]

    assert parse({"start": "2026-12-01T09:30:00.000+02:00", "end": None}) == datetime(
        2026, 12, 1, 9, 30, tzinfo=timezone(timedelta(hours=2))
    )
    assert parse({"start": "2026-12-01", "end": "2026-12-03"}) == DateRange(
        date(2026, 12, 1), date(2026, 12, 3)
    )
    zoned = parse(
        {"start": "2026-12-01T09:00:00", "end": None, "time_zone": "America/New_York"}
    )
    assert zoned == datetime(2026, 12, 1, 9, 0, tzinfo=ZoneInfo("America/New_York"))


# --- Snapshots ---


def test_snapshot_round_trip_is_json_safe(storage):
    storage.save("shot.png", __import__("io").BytesIO(b"png-bytes"))
    values = {
        "Name": "Hello",
        "Number": Decimal("4.25"),
        "Count": 3,
        "Due": date(2026, 12, 1),
        "At": datetime(2026, 12, 1, 9, 30, tzinfo=UTC),
        "Span": DateRange(date(2026, 12, 1), date(2026, 12, 3)),
        "Tags": ("a", "b"),
        "Done": True,
        "Nothing": None,
        "Files": [NotionFile("shot.png")],
    }

    snapshot = encode_snapshot(values)
    restored = decode_snapshot(json.loads(json.dumps(snapshot)))

    assert b"png-bytes" not in json.dumps(snapshot).encode()
    restored_file = restored.pop("Files")[0]
    values.pop("Files")
    assert restored == {**values, "Tags": ["a", "b"]}
    assert restored_file.path == "shot.png"
    assert restored_file.read() == b"png-bytes"


def test_values_that_cannot_be_stored_are_rejected():
    with pytest.raises(NotionValidationError, match='"Thing"'):
        encode_snapshot({"Thing": object()})
