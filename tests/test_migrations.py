import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_migrations_match_the_models():
    # Exits non-zero if a model change has no migration.
    call_command("makemigrations", "notion_sync", "testapp", check=True, dry_run=True)
