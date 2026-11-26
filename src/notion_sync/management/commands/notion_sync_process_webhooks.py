from django.core.management.base import BaseCommand

from notion_sync import webhooks
from notion_sync.management.commands.notion_sync_drain import EVENT_LABELS, summarize


class Command(BaseCommand):
    help = (
        "Process stored webhook events that are due (WEBHOOK_PROCESSING = "
        '"stored"). notion_sync_drain does this too, after the outbox.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Process at most this many events in this run.",
        )

    def handle(self, *args, limit, **options):
        counts = webhooks.process_stored(limit=limit)
        if not counts:
            self.stdout.write("No webhook events to process.")
            return
        self.stdout.write(f"Webhook events: {summarize(counts, EVENT_LABELS)}.")
