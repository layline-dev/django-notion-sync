from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError

from notion_sync.client import NotionClient
from notion_sync.conf import get_settings, get_target
from notion_sync.exceptions import (
    NotionAPIError,
    NotionNotFoundError,
    NotionPermissionError,
)
from notion_sync.mapping import check_mapping, registered
from notion_sync.pages import EXTERNAL_ID_TYPES
from notion_sync.schema import get_schema


class Command(BaseCommand):
    help = (
        "Call Notion and report, for each target, whether the token works, "
        "the data source is shared with the integration, the external ID "
        "property exists, and every registered mapping fits the schema."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--target",
            action="append",
            dest="targets",
            help="Check only this target. Can be given more than once.",
        )

    def handle(self, *args, targets, **options):
        names = targets or list(get_settings().targets)
        if not names:
            raise CommandError("NOTION_SYNC['TARGETS'] is empty.")
        self.problems = 0
        schemas: dict[str, dict] = {}
        for name in names:
            self.stdout.write(f"{name}:")
            self.report(self.check_target(name, schemas))

        mappings = [m for m in registered() if not targets or m.target in names]
        for mapping in mappings:
            self.stdout.write(f"{mapping.model._meta.label} -> {mapping.target}:")
            if mapping.target not in schemas:
                known = mapping.target in get_settings().targets
                self.report(
                    [
                        (
                            False,
                            "The target is not ready; see above."
                            if known
                            else f"NOTION_SYNC['TARGETS'] has no target named "
                            f"{mapping.target!r}.",
                        )
                    ]
                )
                continue
            target, schema = schemas[mapping.target]
            self.report(check_mapping(mapping, target, schema))

        if self.problems:
            raise CommandError(f"{self.problems} problem(s) found.")
        self.stdout.write("All targets are ready.")

    def report(self, results):
        for ok, message in results:
            self.problems += not ok
            style = self.style.SUCCESS if ok else self.style.ERROR
            self.stdout.write(style(f"  {'ok  ' if ok else 'FAIL'}  {message}"))

    def check_target(self, name, schemas):
        try:
            target = get_target(name)
        except ImproperlyConfigured as error:
            yield False, str(error)
            return

        try:
            with NotionClient(target) as client:
                schema = get_schema(target, client, refresh=True)
        except NotionPermissionError as error:
            if error.status == 401:
                yield False, "Notion rejected the token."
            else:
                yield False, f"The token lacks a needed capability: {error}"
            return
        except NotionNotFoundError:
            yield True, "Token accepted."
            yield (
                False,
                "Data source not found. Check DATA_SOURCE_ID and that the "
                "database is shared with the integration.",
            )
            return
        except NotionAPIError as error:
            yield False, f"Could not reach the data source: {error}"
            return

        yield True, "Token accepted."
        yield True, "Data source is shared with the integration."
        schemas[name] = (target, schema)

        if target.external_id_property:
            prop = schema.get(target.external_id_property)
            label = f'External ID property "{target.external_id_property}"'
            if prop is None:
                yield False, f"{label} does not exist in the data source."
            elif prop.type not in EXTERNAL_ID_TYPES:
                yield False, f"{label} is a {prop.type} property; it must be text."
            else:
                yield True, f"{label} exists ({prop.type})."
