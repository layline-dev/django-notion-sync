from django.apps import AppConfig, apps
from django.core.checks import register
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import autodiscover_modules

from notion_sync.checks import ALL_CHECKS


class NotionSyncConfig(AppConfig):
    name = "notion_sync"
    verbose_name = "Notion sync"
    default_auto_field = "django.db.models.BigAutoField"

    def import_models(self) -> None:
        # Without contenttypes, importing the models fails with a RuntimeError
        # that does not name this app, before any system check can run.
        if not apps.is_installed("django.contrib.contenttypes"):
            raise ImproperlyConfigured(
                "notion_sync needs 'django.contrib.contenttypes' in INSTALLED_APPS."
            )
        super().import_models()

    def ready(self) -> None:
        for check in ALL_CHECKS:
            register(check, "notion_sync")
        # Mappings registered in each installed app's notion_mappings.py.
        autodiscover_modules("notion_mappings")
