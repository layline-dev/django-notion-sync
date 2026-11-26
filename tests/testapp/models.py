from django.conf import settings
from django.db import models

from notion_sync.models import NotionSyncMixin


class Ticket(NotionSyncMixin):
    """Mapped to the "inbox" target, which has the duplicate guard."""

    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    priority = models.CharField(max_length=10, blank=True)
    tags = models.JSONField(default=list, blank=True)
    due = models.DateField(null=True, blank=True)
    done = models.BooleanField(default=False)
    link = models.URLField(blank=True)
    screenshot = models.FileField(blank=True)
    status = models.CharField(max_length=20, blank=True)  # Owned by Notion.
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    notes = models.TextField(blank=True)  # Not mapped.

    def __str__(self):
        return self.title


class Note(NotionSyncMixin):
    """Mapped to the "plain" target, which has no external ID property."""

    text = models.CharField(max_length=200)

    def __str__(self):
        return self.text

    def word_count(self):
        return len(self.text.split())


class Unmapped(NotionSyncMixin):
    name = models.CharField(max_length=50)

    def __str__(self):
        return self.name


class NotMixin(models.Model):
    name = models.CharField(max_length=50)

    def __str__(self):
        return self.name
