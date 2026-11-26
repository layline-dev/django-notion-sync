"""Settings for the test project.

SQLite in memory by default. Set NOTION_SYNC_TEST_DB=postgres to run against
Postgres, configured with the standard PG* environment variables.
"""

import os

SECRET_KEY = "test-only"
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
ROOT_URLCONF = "tests.urls"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "notion_sync",
    "tests.testapp",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

TEST_DB = os.environ.get("NOTION_SYNC_TEST_DB", "sqlite")

if TEST_DB == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("PGDATABASE", "notion_sync"),
            "USER": os.environ.get("PGUSER", "postgres"),
            "PASSWORD": os.environ.get("PGPASSWORD", ""),
            "HOST": os.environ.get("PGHOST", "localhost"),
            "PORT": os.environ.get("PGPORT", "5432"),
        }
    }
elif TEST_DB == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }
else:
    raise RuntimeError(
        f"NOTION_SYNC_TEST_DB must be 'sqlite' or 'postgres', not {TEST_DB!r}"
    )

CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
}
# The tests run in one process, so an unshared cache is fine here.
SILENCED_SYSTEM_CHECKS = ["notion_sync.W002"]

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

INBOX_TOKEN = "secret-inbox-token"
PLAIN_TOKEN = "secret-plain-token"
INBOX_WEBHOOK_TOKEN = "secret_inbox-webhook-verification"
PLAIN_WEBHOOK_TOKEN = "secret_plain-webhook-verification"

NOTION_SYNC = {
    "TARGETS": {
        # A target with the duplicate guard and upsert.
        "inbox": {
            "TOKEN": INBOX_TOKEN,
            "DATA_SOURCE_ID": "ds-inbox",
            "EXTERNAL_ID_PROPERTY": "External ID",
            "WEBHOOK_VERIFICATION_TOKEN": INBOX_WEBHOOK_TOKEN,
        },
        # A target without an external ID property.
        "plain": {
            "TOKEN": PLAIN_TOKEN,
            "DATA_SOURCE_ID": "ds-plain",
            "WEBHOOK_VERIFICATION_TOKEN": PLAIN_WEBHOOK_TOKEN,
        },
    },
}
