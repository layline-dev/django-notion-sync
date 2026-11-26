import re
from datetime import date

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import ContentFile

import notion_sync
from notion_sync import In, NotionMapping, Out
from notion_sync.files import NotionFile
from notion_sync.mapping import get_mapping, registered, require_mapping
from tests.testapp.models import Note, NotMixin, Ticket, Unmapped


def test_the_test_apps_mappings_are_autodiscovered():
    assert [mapping.model for mapping in registered()] == [Note, Ticket]
    assert get_mapping(Ticket).target == "inbox"
    assert get_mapping(Ticket()) is get_mapping(Ticket)
    assert get_mapping(Unmapped) is None


def test_require_mapping_names_the_model():
    with pytest.raises(ImproperlyConfigured, match="testapp.Unmapped has no Notion"):
        require_mapping(Unmapped)


def test_register_returns_the_class_and_unregister_removes_it(register):
    mapping = register(Unmapped)

    assert isinstance(mapping, NotionMapping)
    assert mapping.model is Unmapped
    notion_sync.unregister(Unmapped)
    assert get_mapping(Unmapped) is None
    notion_sync.unregister(Unmapped)  # A second call does nothing.


@pytest.mark.parametrize(
    ("attrs", "message"),
    [
        ({"target": ""}, "set `target`"),
        ({"fields": {}}, "set `fields`"),
        ({"fields": {"Name": "name"}}, '"Name" must map to Out() or In()'),
        ({"fields": {"": Out("name")}}, "non-empty strings"),
        ({"fields": {"Name": Out("nope")}}, "has no attribute 'nope'"),
        ({"fields": {"Name": In("nope")}}, "'nope', which is not a field"),
        ({"fields": {"Name": In("id")}}, "cannot write to 'id'"),
        ({"fields": {"Name": In("notion_page_id")}}, "cannot write to"),
        (
            {"fields": {"A": Out("name"), "B": In("name")}},
            "both sent to Notion and written back",
        ),
        ({"auto_sync": "yes"}, "auto_sync must be True, False or None"),
        ({"on_delete": "delete"}, "on_delete must be one of nothing, archive"),
        ({"on_notion_trash": "x"}, "on_notion_trash must be one of"),
        ({"watch_fields": "name"}, "watch_fields must be a list"),
        ({"watch_fields": ["nope"]}, "'nope' is not a field"),
    ],
)
def test_register_rejects_a_bad_mapping(register, attrs, message):
    with pytest.raises(ImproperlyConfigured, match=re.escape(message)):
        register(Unmapped, **attrs)
    assert get_mapping(Unmapped) is None


def test_register_rejects_models_it_cannot_sync(register):
    with pytest.raises(ImproperlyConfigured, match="must inherit from NotionSyncMixin"):
        register(NotMixin)
    with pytest.raises(ImproperlyConfigured, match="already has a Notion mapping"):
        register(Ticket)
    with pytest.raises(ImproperlyConfigured, match="decorates a subclass"):
        notion_sync.register(Unmapped)(object)
    with pytest.raises(ImproperlyConfigured, match="at least one model"):
        notion_sync.register()


def test_out_and_in_check_their_arguments():
    with pytest.raises(ImproperlyConfigured):
        Out("")
    with pytest.raises(ImproperlyConfigured):
        Out(3)
    with pytest.raises(ImproperlyConfigured):
        In("")


@pytest.mark.django_db
def test_out_reads_fields_paths_methods_and_callables():
    user = User.objects.create(username="sam", email="sam@example.com")
    ticket = Ticket(title="T", reporter=user, due=date(2026, 12, 1))
    note = Note(text="three short words")

    assert Out("title").value(ticket) == "T"
    assert Out("due").value(ticket) == date(2026, 12, 1)
    assert Out("reporter.email").value(ticket) == "sam@example.com"
    assert Out("reporter.email").value(Ticket(title="no reporter")) is None
    assert Out("word_count").value(note) == 3
    assert Out(lambda n: n.text.upper()).value(note) == "THREE SHORT WORDS"


@pytest.mark.django_db
def test_out_turns_a_file_field_into_a_notion_file(storage):
    ticket = Ticket(title="T")
    assert Out("screenshot").value(ticket) == []  # An empty file clears it.

    ticket.screenshot.save("shot.png", ContentFile(b"png"), save=False)
    value = Out("screenshot").value(ticket)

    assert isinstance(value, NotionFile)
    assert value.path == ticket.screenshot.name
    assert value.storage_alias == "default"
    listed = Out(lambda t: [t.screenshot, Ticket().screenshot]).value(ticket)
    assert [item.path for item in listed] == [ticket.screenshot.name]


def test_get_properties_leaves_out_create_only_fields_on_update():
    mapping = get_mapping(Ticket)
    ticket = Ticket(title="T")

    assert "Files" in mapping.get_properties(ticket, creating=True)
    assert "Files" not in mapping.get_properties(ticket, creating=False)
    assert "Status" not in mapping.get_properties(ticket, creating=True)


def test_the_default_external_id_is_the_model_label_and_primary_key():
    assert get_mapping(Ticket).get_external_id(Ticket(pk=42)) == "testapp.ticket:42"


def test_options_fall_back_to_the_project_defaults(settings, register):
    mapping = get_mapping(Ticket)
    assert mapping.get_auto_sync() is False
    assert mapping.get_on_delete() == "nothing"
    assert mapping.get_on_notion_trash() == "nothing"

    settings.NOTION_SYNC = {
        **settings.NOTION_SYNC,
        "AUTO_SYNC": True,
        "ON_DELETE": "archive",
        "ON_NOTION_TRASH": "unlink",
    }
    assert mapping.get_auto_sync() is True
    assert mapping.get_on_delete() == "archive"
    assert mapping.get_on_notion_trash() == "unlink"

    own = register(Unmapped, auto_sync=False, on_delete="nothing")
    assert own.get_auto_sync() is False
    assert own.get_on_delete() == "nothing"


def test_watched_fields_come_from_plain_field_sources(register):
    assert get_mapping(Ticket).watched() == {
        "title",
        "body",
        "priority",
        "tags",
        "due",
        "done",
        "link",
        "screenshot",
    }
    # A method source cannot be watched, so every save counts.
    assert get_mapping(Note).watched() is None

    mapping = register(
        Unmapped, fields={"Name": Out(lambda u: u.name)}, watch_fields=["name"]
    )
    assert mapping.watched() == {"name"}
