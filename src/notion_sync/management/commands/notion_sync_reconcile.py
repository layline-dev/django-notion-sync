from datetime import date, datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from notion_sync import inbound
from notion_sync.conf import get_settings
from notion_sync.exceptions import NotionAPIError


def parse_since(value):
    try:
        if len(value) == 10:
            moment = datetime.combine(date.fromisoformat(value), time())
        else:
            moment = datetime.fromisoformat(value)
    except ValueError:
        raise CommandError(
            f"--since must be an ISO 8601 date or datetime, not {value!r}."
        ) from None
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment)
    return moment


class Command(BaseCommand):
    help = (
        "Read pages edited in Notion recently and apply their In fields to "
        "linked records. The window is RECONCILE_LOOKBACK_HOURS unless --since "
        "or --all is given. Pages in the trash are not seen."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--target",
            action="append",
            dest="targets",
            help="Reconcile only this target. Can be given more than once.",
        )
        window = parser.add_mutually_exclusive_group()
        window.add_argument(
            "--since",
            help="Read pages edited at or after this ISO 8601 date or datetime "
            "(a naive value is in the current time zone).",
        )
        window.add_argument(
            "--all",
            action="store_true",
            dest="all_pages",
            help="Read every page in the data source.",
        )

    def handle(self, *args, targets, since, all_pages, **options):
        names = targets or list(get_settings().targets)
        if not names:
            raise CommandError("NOTION_SYNC['TARGETS'] is empty.")
        if all_pages:
            start = None
        elif since:
            start = parse_since(since)
        else:
            start = inbound.default_since()
        failures = 0
        for name in names:
            try:
                read, changed = inbound.reconcile(name, since=start)
            except NotionAPIError as error:
                failures += 1
                self.stderr.write(self.style.ERROR(f"{name}: {error}"))
                continue
            self.stdout.write(f"{name}: read {read} pages, updated {changed} records.")
        if failures:
            raise CommandError(f"{failures} target(s) could not be reconciled.")
