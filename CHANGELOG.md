# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-11-25

First public release.

### Added

- Writing pages: `notion_sync.api.create_page`, `update_page`, `upsert_page`,
  `archive_page` and `get_page`, taking plain Python values converted by the
  data source's schema.
- File attachments through `NotionFile`, uploaded with Notion's upload API.
- The outbox: `enqueue_create`, `enqueue_update`, `enqueue_upsert` and
  `enqueue_archive`, with backoff, an attempt cap and a guard against
  duplicate pages after a lost response. Rows end as `sent`, `failed`,
  `needs_review` or `skipped`.
- Model sync: `NotionSyncMixin` stores a record's page ID, with
  `sync_to_notion()` and `enqueue_notion_sync()`. A queued sync stores a
  reference and reads values when sent, and a record has at most one pending
  row.
- Declarative mappings: `@notion_sync.register(Model)` with `Out()` and
  `In()` fields, found in each app's `notion_mappings.py`, with `auto_sync`,
  `on_delete = "archive"` and `on_notion_trash` (`"unlink"` or `"delete"`).
- Inbound sync: `In()` fields are written back from Notion. Pages are always
  fetched fresh, older states are skipped, and the save names only `In`
  fields, so it never queues a sync back to Notion.
- A webhook endpoint, `include("notion_sync.urls")`. It checks
  `X-Notion-Signature` against each target's `WEBHOOK_VERIFICATION_TOKEN`,
  routes events by the page's data source, ignores repeated events and events
  older than 48 hours, and acts on `page.created`, `page.properties_updated`,
  `page.moved`, `page.deleted` and `page.undeleted`.
- `WEBHOOK_PROCESSING = "stored"`: events are saved as `WebhookEvent` rows and
  processed later, with backoff and an attempt cap.
- Management commands: `notion_sync_check` (calls Notion to check targets and
  mappings), `notion_sync_drain`, `notion_sync_process_webhooks`,
  `notion_sync_reconcile [--target] [--since | --all]`, `notion_sync_prune`
  (outbox rows and processed webhook events) and `notion_sync_webhook_token`.
- The `celery` extra: tasks `drain_outbox`, `send_outbox_item`,
  `process_webhook_event`, `process_webhook_events` and `reconcile`, with
  `DISPATCH = "celery"` sending each row as soon as it commits.
- Signals `page_synced`, `sync_failed`, `page_changed` and `page_trashed`.
- Admin for the outbox, with a retry action, and a read-only admin for webhook
  events, with a superuser-only page showing the webhook handshake token.
- The `NOTION_SYNC` setting with named targets.
- Django system checks, `notion_sync.E001` to `E004` and `W001` to `W002`, for
  the setting, mapping targets, Celery dispatch and the webhook cache. They
  run offline. A clear `ImproperlyConfigured` at startup when
  `django.contrib.contenttypes` is not installed.
- Type hints: the package ships `py.typed`, and its public modules are fully
  typed and checked with mypy.
- A documented public API and deprecation policy in the README. Each public
  module defines `__all__`.

[Unreleased]: https://github.com/layline-dev/django-notion-sync/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/layline-dev/django-notion-sync/releases/tag/v0.1.0
