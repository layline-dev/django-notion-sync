"""Found by autodiscovery when the test project starts."""

import notion_sync
from notion_sync import In, NotionMapping, Out
from tests.testapp.models import Note, Ticket


@notion_sync.register(Ticket)
class TicketMapping(NotionMapping):
    target = "inbox"
    fields = {
        "Name": Out("title"),
        "Text": Out("body"),
        "Select": Out("priority"),
        "Tags": Out("tags"),
        "Due": Out("due"),
        "Done": Out("done"),
        "Link": Out("link"),
        "Files": Out("screenshot", create_only=True),
        "Status": In("status"),
    }


@notion_sync.register(Note)
class NoteMapping(NotionMapping):
    target = "plain"
    fields = {
        "Name": Out("text"),
        "Number": Out("word_count"),
    }
