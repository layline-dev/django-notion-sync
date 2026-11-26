from django.core.management.base import BaseCommand

from notion_sync import outbox, webhooks

OUTBOX_LABELS = {
    "succeeded": "sent",
    "pending": "will retry",
    "failed": "failed",
    "needs_review": "need review",
    "skipped": "skipped",
}
EVENT_LABELS = {
    "processed": "processed",
    "pending": "will retry",
    "failed": "failed",
}


def summarize(counts, labels):
    return ", ".join(
        f"{counts[status]} {label}"
        for status, label in labels.items()
        if counts[status]
    )


class Command(BaseCommand):
    help = (
        "Send the queued Notion writes that are due, then process stored "
        "webhook events."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Handle at most this many outbox rows, and this many webhook "
            "events, in this run.",
        )

    def handle(self, *args, limit, **options):
        counts = outbox.drain(limit=limit)
        self.stdout.write(
            summarize(counts, OUTBOX_LABELS) if counts else "Nothing to send."
        )
        events = webhooks.process_stored(limit=limit)
        if events:
            self.stdout.write(f"Webhook events: {summarize(events, EVENT_LABELS)}.")
