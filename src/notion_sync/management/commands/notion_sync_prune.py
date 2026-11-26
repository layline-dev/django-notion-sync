from django.core.management.base import BaseCommand

from notion_sync import outbox, webhooks


class Command(BaseCommand):
    help = (
        "Delete outbox rows sent or skipped, and webhook events processed, more "
        "than RETENTION_DAYS ago. Failed rows and events stay until someone "
        "deals with them. Webhook events are kept at least 48 hours."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="Override NOTION_SYNC['RETENTION_DAYS'] for this run.",
        )

    def handle(self, *args, days, **options):
        deleted = outbox.prune(days=days)
        events = webhooks.prune(days=days)
        self.stdout.write(
            f"Deleted {deleted} sent rows and {events} processed webhook events."
        )
