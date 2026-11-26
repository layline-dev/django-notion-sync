import importlib
import re
import subprocess
import sys

import pytest
from django.apps import apps
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.db import connection
from django.dispatch import Signal

import notion_sync
from notion_sync.apps import NotionSyncConfig

WITHOUT_CONTENTTYPES = """
import django
from django.conf import settings

settings.configure(
    INSTALLED_APPS=["notion_sync"],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
)
django.setup()
"""

PUBLIC_MODULES = [
    "notion_sync.api",
    "notion_sync.exceptions",
    "notion_sync.files",
    "notion_sync.mapping",
    "notion_sync.models",
    "notion_sync.pages",
    "notion_sync.signals",
    "notion_sync.tasks",
]


def test_app_is_installed_in_the_test_project():
    config = apps.get_app_config("notion_sync")

    assert isinstance(config, NotionSyncConfig)
    assert config.module is notion_sync


def test_version_is_a_release_string():
    assert re.fullmatch(r"\d+\.\d+\.\d+([a-z]+\d+)?", notion_sync.__version__)


@pytest.mark.django_db  # Django 6.1 asks the database whether it supports JSON.
def test_system_checks_pass():
    call_command("check", fail_level="WARNING")


@pytest.mark.django_db
def test_database_backend_matches_the_requested_one():
    expected = {"sqlite": "sqlite", "postgres": "postgresql"}[settings.TEST_DB]

    assert connection.vendor == expected
    assert ContentType.objects.filter(app_label="auth").exists()


def test_the_package_re_exports_only_names_that_exist():
    for name in notion_sync.__all__:
        assert hasattr(notion_sync, name), name


@pytest.mark.parametrize("module_name", PUBLIC_MODULES)
def test_each_public_module_lists_the_names_it_defines(module_name):
    module = importlib.import_module(module_name)

    assert module.__all__ == sorted(module.__all__)
    for name in module.__all__:
        value = getattr(module, name)
        if isinstance(value, Signal):
            continue  # An instance; its __module__ is Signal's.
        assert value.__module__ == module_name, name


def test_a_missing_contenttypes_app_is_named_at_startup():
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", WITHOUT_CONTENTTYPES],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert (
        "ImproperlyConfigured: notion_sync needs 'django.contrib.contenttypes'"
        in result.stderr
    )
