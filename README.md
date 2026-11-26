# django-notion-sync

A reusable Django app that creates and updates pages in a Notion database.
Call it inline, or queue the write in an outbox and let a command send it with
retries. Edits made in Notion can flow back to your models through webhooks,
a reconcile command, or both.

> **Status: early.** The API may still change before 1.0. See
> [Public API](#public-api) for what is covered and how changes are announced.

It writes page properties and file attachments. It does not write page
content, icons or covers.

## Compatibility

| | Supported |
|---|---|
| Python | 3.11, 3.12, 3.13, 3.14 |
| Django | 5.2, 6.0, 6.1 (Django 6.x needs Python 3.12+) |
| Databases tested | SQLite, PostgreSQL |
| Notion API version | `2025-09-03` |
| Notion SDK | [`notion-client`](https://pypi.org/project/notion-client/) 3.x |

## Install

```console
pip install django-notion-sync
```

Use `pip install "django-notion-sync[celery]"` to also install Celery.

Add the app and run its migration. `django.contrib.contenttypes` must be
installed too.

```python
INSTALLED_APPS = [
    # ...
    "django.contrib.contenttypes",
    "notion_sync",
]
```

```console
python manage.py migrate notion_sync
```

## Set up Notion

1. Create an internal integration at <https://www.notion.so/profile/integrations>
   with the **Read content**, **Insert content** and **Update content**
   capabilities, and copy its token.
2. Share the database with the integration (database menu → Connections).
3. Copy the database's **data source ID** (database menu → Manage data
   sources → Copy data source ID). This is not the database ID in the URL.
4. Optional but recommended: add a text property, for example `External ID`.
   It enables upsert and protects queued creates against duplicates.

## Settings

```python
NOTION_SYNC = {
    "TARGETS": {
        "inbox": {
            "TOKEN": os.environ["NOTION_INBOX_TOKEN"],
            "DATA_SOURCE_ID": "2f26ee68-df30-4251-aad4-8ddc420cba3d",
            "EXTERNAL_ID_PROPERTY": "External ID",  # optional
            "WEBHOOK_VERIFICATION_TOKEN": os.environ.get(  # optional, for webhooks
                "NOTION_INBOX_WEBHOOK_TOKEN"
            ),
        },
    },
}
```

A target is one data source plus the token that can write to it. You refer to
it by name in every call. Keep tokens in the environment; the package never
writes a token to the database, a log line or an exception message.

Optional settings, with their defaults:

| Setting | Default | Meaning |
|---|---|---|
| `NOTION_VERSION` | `"2025-09-03"` | The `Notion-Version` header sent with every request. |
| `TIMEOUT_SECONDS` | `30` | Timeout for each request to Notion. |
| `MAX_ATTEMPTS` | `8` | Attempts before a queued write is marked failed. |
| `RETENTION_DAYS` | `7` | How long sent outbox rows and processed webhook events are kept before `notion_sync_prune` deletes them. |
| `SCHEMA_CACHE_SECONDS` | `300` | How long a data source's schema stays in Django's cache. `0` turns the cache off. |
| `SENDING_TIMEOUT_SECONDS` | `600` | After this long, a row still marked as sending is treated as interrupted. |
| `DISPATCH` | `"command"` | `"celery"` sends each queued row from a Celery task as soon as it commits. |
| `AUTO_SYNC` | `False` | Default for mappings: queue a sync after a save that changed a mapped field. |
| `ON_DELETE` | `"nothing"` | Default for mappings: `"archive"` moves the page to the trash when the record is deleted. |
| `ON_NOTION_TRASH` | `"nothing"` | Default for mappings, when a page is moved to the trash in Notion: `"unlink"` clears the record's page ID, `"delete"` deletes the record. |
| `WEBHOOK_PROCESSING` | `"inline"` | `"stored"` saves each webhook event and processes it later, from the drain command or Celery. |
| `RECONCILE_LOOKBACK_HOURS` | `24` | How far back `notion_sync_reconcile` looks by default. `0` reads every page. |

Then check the setup. This calls Notion:

```console
python manage.py notion_sync_check
```

### System checks

The package also adds Django system checks, which run with `manage.py check`,
`runserver` and `migrate`, and never call Notion. Run only these with
`manage.py check --tag notion_sync`.

| ID | Problem |
|---|---|
| `notion_sync.E001` | `NOTION_SYNC` is invalid: an unknown key, or a value of the wrong type or out of range. |
| `notion_sync.E002` | A target is invalid: not a dict, an unknown key, or a value of the wrong type. |
| `notion_sync.E003` | A mapping's `target` is not in `TARGETS`. |
| `notion_sync.E004` | `DISPATCH` is `"celery"`, but Celery is not installed. |
| `notion_sync.W001` | A target has no `TOKEN` or `DATA_SOURCE_ID`. It is a warning, not an error, so a build or CI job without the environment variables still runs. |
| `notion_sync.W002` | `notion_sync.urls` is installed, but the default cache is `LocMemCache` or `DummyCache`. The webhook handshake token and the IDs of events already seen must be shared by every process. |

A single-process development server can silence W002 with
`SILENCED_SYSTEM_CHECKS = ["notion_sync.W002"]`. Without
`django.contrib.contenttypes` in `INSTALLED_APPS`, the app refuses to start
and says why.

## Writing pages

```python
from datetime import date

from notion_sync.api import (
    create_page,
    update_page,
    upsert_page,
    archive_page,
    get_page,
)
from notion_sync.files import NotionFile

page = create_page(
    "inbox",
    {
        "Name": "Checkout button does nothing",
        "Priority": "High",
        "Due": date(2026, 12, 1),
        "Tags": ["bug", "web"],
        "Screenshot": NotionFile(ticket.screenshot),
    },
)

update_page("inbox", page.id, {"Priority": "Low"})
upsert_page("inbox", "ticket-42", {"Name": "Created or updated by external ID"})
archive_page("inbox", page.id)  # moves the page to the trash
page = get_page("inbox", page.id)  # values come back as plain Python
```

A returned `Page` has `id`, `url`, `last_edited_time`, `properties`,
`in_trash` and `data_source_id`.

These functions call Notion immediately and raise if it fails. They are not
retried. To have failures retried, queue the write instead (see below).

### Values

Keys are Notion property names. The package reads the data source's schema
and converts each value by the property's type:

| Notion type | Python value |
|---|---|
| title, text | `str`. Long text is split into 2,000-character pieces for you. |
| number | `int`, `float` or `Decimal` |
| select, status | the option's name |
| multi-select | a list of option names |
| date | `date`, `datetime`, or `DateRange(start, end)` from `notion_sync.values` |
| checkbox | `bool` |
| URL, email, phone | `str` |
| person | a list of Notion user IDs |
| relation | a list of page IDs |
| files & media | a `NotionFile`, an `https://` URL, or a list of them |

`None` clears a property (an empty list for the list types). A naive
`datetime` is read in Django's current time zone.

A new select or multi-select option name is created in Notion when first used;
option names cannot contain commas. A status option must already exist.

Formula, rollup, created/edited time, created/edited by and unique ID
properties can be read but not written.

A value that does not fit raises `NotionValidationError`, naming the property,
its Notion type and the value, before any page is written.

### Files

`NotionFile` takes a model's file field value or a storage path:

```python
NotionFile(ticket.screenshot)
NotionFile("uploads/report.pdf")  # in the default storage
NotionFile("uploads/report.pdf", storage="private")  # an alias in STORAGES
```

The bytes are uploaded to Notion, so the file needs no public URL. Notion
limits files to 5 MiB on free workspaces. Writing a files property replaces
every file already on it.

### External IDs and upsert

With `EXTERNAL_ID_PROPERTY` set on the target, `create_page(...,
external_id="ticket-42")` stores your ID on the page and `upsert_page` finds
the page by it. Notion's search can lag a few seconds behind a write, so two
upserts of a brand-new ID in quick succession can create two pages.

## The outbox

Queue a write when it must not be lost or must not slow down a request:

```python
from notion_sync.api import (
    enqueue_create,
    enqueue_update,
    enqueue_upsert,
    enqueue_archive,
)

enqueue_create("inbox", {"Name": "Queued", "Screenshot": NotionFile(ticket.screenshot)})
```

Enqueueing only writes a database row. It makes no request to Notion, so it
works during a Notion outage and takes part in the surrounding transaction.
A queued file is stored as its storage path, never its bytes, so it must
already be saved to a storage listed in `STORAGES`.

Send the queue with the drain command. Run it every minute from cron or a
scheduler:

```cron
* * * * * cd /srv/app && python manage.py notion_sync_drain
0 3 * * * cd /srv/app && python manage.py notion_sync_prune
```

Several drains can run at once on PostgreSQL; each row is sent by only one.

### What happens on failure

| Failure | Result |
|---|---|
| Rate limit (429) | Retried after the wait Notion asks for. |
| Server error, timeout, conflict, network down | Retried with a doubling wait: 30 s, 1 min, 2 min … up to 1 hour. |
| Value does not fit, permission denied, page not found | Marked `failed` at once. |
| Still failing after `MAX_ATTEMPTS` | Marked `failed`. |
| The record was deleted, or a newer row replaced this one | Marked `skipped`. |

The outbox owns every retry: the Notion SDK's own retries are turned off, so
one attempt is one request.

**Duplicate pages.** Notion can create a page and the response can still be
lost. If the target has `EXTERNAL_ID_PROPERTY`, every queued create writes an
ID to it (yours, or the outbox row's), and a retry first looks for a page with
that ID. If the target has none, a create with an unknown outcome is marked
`needs_review` and is not sent again; check the database, then delete the row
or retry it from the admin.

Values in a queued write are checked against the Notion schema when the row is
sent, not when it is queued.

### Signals

In `notion_sync.signals`:

- `page_synced`: a write succeeded, inline or queued. Arguments: `target`,
  `operation`, `page_id`, `page`, `item` (the outbox row, or `None`) and
  `instance` (the model instance for a model sync, or `None`). For a model
  sync the sender is the model class.
- `sync_failed`: an outbox row was marked `failed` or `needs_review`.
  Arguments: `item`, `error`.

```python
from django.dispatch import receiver
from notion_sync.signals import sync_failed


@receiver(sync_failed)
def alert(sender, item, error, **kwargs):
    logger.error("Notion sync failed for %s: %s", item.pk, error)
```

## Syncing models

Add the mixin to a model, then make and run a migration. It adds
`notion_page_id`, `notion_synced_at` and `notion_last_edited_time`.

```python
from notion_sync.models import NotionSyncMixin


class Ticket(NotionSyncMixin, models.Model):
    title = models.CharField(max_length=200)
    priority = models.CharField(max_length=10, blank=True)
    screenshot = models.FileField(blank=True)
    status = models.CharField(max_length=20, blank=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    tags = models.ManyToManyField("Tag", blank=True)
```

Map it in a `notion_mappings.py` module in the same app. The package imports
that module from every installed app at startup.

```python
import notion_sync
from notion_sync import In, NotionMapping, Out

from .models import Ticket


@notion_sync.register(Ticket)
class TicketMapping(NotionMapping):
    target = "inbox"
    fields = {
        "Name": Out("title"),
        "Priority": Out("priority"),
        "Reporter": Out(lambda ticket: ticket.user.email),
        "Screenshot": Out("screenshot", create_only=True),
        "Status": In("status"),
    }
    auto_sync = True  # None (the default) uses the AUTO_SYNC setting.
    on_delete = "archive"  # None uses the ON_DELETE setting.
    on_notion_trash = "unlink"  # None uses the ON_NOTION_TRASH setting.
```

- `Out(source)` sends a value to Notion. `source` is a field or attribute
  name, a dotted path such as `"user.email"`, a method name (it is called),
  or a callable that takes the instance. A file field becomes a `NotionFile`.
  An empty string becomes `None`, which clears the property.
- `Out(..., create_only=True)` is sent when the page is created and never
  again. Use it for files, which are otherwise uploaded on every sync.
- `In(field)` marks a property Notion owns. Its value is written to the
  field when the page changes in Notion (see
  [Syncing Notion edits back](#syncing-notion-edits-back)) and is never sent.
  A field cannot be both sent and received.

Then:

```python
ticket.sync_to_notion()  # now; raises on failure; returns the Page
ticket.enqueue_notion_sync()  # through the outbox
```

The first sync creates the page and stores its ID on the record; later syncs
update that page. The record is updated with a query, not `save()`, so
linking never triggers another sync.

A queued sync stores a reference to the record, not its values, and reads
them when the row is sent. A record has at most one pending row: queue it five
times before the drain runs and it is still one write, carrying the latest
values. If the record is deleted before then, the row is skipped.

If the target has `EXTERNAL_ID_PROPERTY`, the record's external ID is
`<app label>.<model name>:<primary key>` (override `get_external_id()` to
change it). A create first looks for a page with that ID, so a page made by an
attempt whose response was lost is found and linked, not created twice.

### Automatic sync

With `auto_sync` on, a save that changed an `Out` field queues a sync. A
change is judged by the fields the `Out` sources name. If any source is a
callable, a dotted path or a method, every save counts; set `watch_fields`
on the mapping to list the fields that matter. A save with `update_fields`
that names only `In` fields does not count.

The row is written in the same transaction as the save, so a rolled-back save
queues nothing. `QuerySet.update()`, `bulk_update()`, `bulk_create()` and
fixture loading do not call `save()` and are not seen; queue those records
yourself.

Only saves of the mapped model itself are seen. Adding or removing a
many-to-many value, or saving a related object that an `Out` source reads
(such as the `User` behind `"user.email"`), queues nothing. Queue those
records from your own receiver:

```python
from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver


@receiver(m2m_changed, sender=Ticket.tags.through)
def tags_changed(sender, instance, action, **kwargs):
    if action in ("post_add", "post_remove", "post_clear"):
        instance.enqueue_notion_sync()


@receiver(post_save, sender=User)
def user_changed(sender, instance, **kwargs):
    for ticket in Ticket.objects.filter(user=instance):
        ticket.enqueue_notion_sync()
```

`enqueue_notion_sync()` writes in the current transaction, and a record with
a pending row is not queued twice.

### Deletes

With `on_delete = "archive"`, deleting a record queues moving its page to the
trash. A pending sync for the record becomes the archive. A record that was
never sent to Notion queues nothing. Deleting through a queryset works too:
Django sends the delete signal for each row.

### Checking mappings

`notion_sync_check` also checks every mapping against its target's schema:
each property exists, can be written (for `Out`), and suits the model
field's type.

## Syncing Notion edits back

Mark the properties Notion owns with `In(field)` on a mapping. When a page
changes in Notion, the package fetches it, finds the records linked to it by
`notion_page_id`, and writes each `In` property to its field. There are two
ways to hear about changes, and they work together:

- **Reconcile**: a command you schedule. It needs no public URL.
- **Webhooks**: Notion calls your site within seconds of a change.

Running reconcile on a schedule is a good safety net even with webhooks, as
Notion does not promise to deliver every event.

### What applying a change does

- Only `In` fields and `notion_last_edited_time` are written, with
  `save(update_fields=...)`. That save never queues a sync back to Notion,
  even with `auto_sync`, so the echo of your own write changes nothing.
- A value that does not fit the field (too long, not a valid choice, empty
  for a field that cannot be empty) is left unchanged and logged as a warning.
- Notion's events can arrive late or out of order, so the page is always
  fetched fresh. If a record already holds a newer state of the page, an
  older one is skipped. Notion keeps edit times only to the minute, so two
  states with the same edit time cannot be ordered: if one would change a
  field, the page is read again from Notion while the record is locked, and
  that fresh read is applied. A second edit in the same minute is not lost.
- When a field changes, the record is saved with `save()`, so your own
  `post_save` receivers see it. When only the page's edit time moved, the
  record is updated with a query, so Django sends no `post_save`
  (`page_changed` is still sent).

### Reconcile without a public URL

```console
python manage.py notion_sync_reconcile                 # pages edited in the last RECONCILE_LOOKBACK_HOURS
python manage.py notion_sync_reconcile --target inbox --since 2026-11-20T08:00
python manage.py notion_sync_reconcile --all           # every page
```

Run it from cron, as often as you want changes to arrive:

```cron
*/5 * * * * cd /srv/app && python manage.py notion_sync_reconcile
```

Re-reading the same window is harmless: a page already applied changes
nothing and sends no signal. Reconcile cannot see pages in the trash (Notion
leaves them out of queries), so `on_notion_trash` and `page_trashed` need
webhooks.

### Setting up webhooks

1. Include the URL. The endpoint accepts POST only and needs no login or CSRF
   token; every request is checked against Notion's signature instead.

   ```python
   urlpatterns = [
       # ...
       path("notion/webhook/", include("notion_sync.urls")),
   ]
   ```

2. Deploy, so Notion can reach the URL over HTTPS.
3. In Notion, open the integration at
   <https://www.notion.so/profile/integrations>, go to the **Webhooks** tab
   and create a subscription:
   - URL: `https://your.site/notion/webhook/`
   - API version: **2025-09-03** or later. Older versions leave the data
     source out of events, and the package ignores those events (with a
     warning in the log).
   - Events: `page.created`, `page.properties_updated`, `page.moved`,
     `page.deleted` and `page.undeleted`. Other events are accepted and
     ignored.
4. Notion sends a one-time verification token to the URL. Within 15 minutes,
   show it with:

   ```console
   python manage.py notion_sync_webhook_token
   ```

   or, as a superuser, in the admin: **Webhook events → Webhook verification
   token**. Other staff cannot open that page.

   The token is held in Django's cache, so the cache must be shared by the
   web processes and wherever you run the command (a local-memory cache is
   not). If nothing arrived, click **Resend token** in Notion. The token is
   never logged, and the package stores it nowhere else.
5. Paste the token into Notion's **Verify** dialog, and set it as the
   target's `WEBHOOK_VERIFICATION_TOKEN` (from the environment). Every target
   that shares the integration uses the same token. Redeploy.

   Until the setting is live, events are answered with 401. Notion retries a
   failed delivery for about a day, so they arrive once you have redeployed,
   and reconcile catches anything else. The package does not accept a token
   from the cache in the meantime: the handshake is not signed, so that would
   let anyone who posted a token sign events.

If more than one token is shown, someone else posted to the URL too: the
handshake request is not signed. Only the token Notion accepts in its Verify
dialog is real.

From then on each request must carry a valid `X-Notion-Signature`, or it is
answered with 401. An event is routed to the target whose data source holds
the page; events for other data sources are ignored. Each event is handled
once: a repeated delivery of the same event, or one older than 48 hours, is
ignored.

### Processing events inline or later

With `WEBHOOK_PROCESSING = "inline"` (the default), the page is fetched and
applied while Notion waits. If Notion cannot be reached the view answers 503
and Notion delivers the event again later. Seen event IDs are kept in Django's
cache for 48 hours; use a cache shared by all web processes.

With `"stored"`, the view saves a `WebhookEvent` and answers at once. Process
the stored events with either command (the drain does the outbox first, then
the events):

```cron
* * * * * cd /srv/app && python manage.py notion_sync_drain
* * * * * cd /srv/app && python manage.py notion_sync_process_webhooks  # events only
```

With `DISPATCH = "celery"`, each stored event is also processed by a task as
soon as it is saved. A failed event is retried with the outbox's backoff up
to `MAX_ATTEMPTS`, then marked failed and left for you in the admin.

### When a page is trashed in Notion

A `page.deleted` event sends `page_trashed`, then applies the mapping's
`on_notion_trash`:

| Value | Result |
|---|---|
| `"nothing"` | The record keeps its page ID. |
| `"unlink"` | The record's `notion_page_id` is cleared. Its next sync creates a new page. Restoring the page in Notion does not link it again. |
| `"delete"` | The record is deleted. No archive is queued for it, even with `on_delete = "archive"`, as the page is already in the trash. |

### Inbound signals

In `notion_sync.signals`:

- `page_changed`: a page changed in Notion. Arguments: `target`, `page` (the
  fetched `Page`), `event_type` (such as `"page.properties_updated"`, or
  `"reconcile"`), `instance` (the linked record, or `None`) and
  `changed_fields` (the model fields that were updated, possibly empty). For
  a linked record the sender is its model class. It is sent when an `In`
  field changed or the page's edit time moved on, and for every webhook
  event about a page no record is linked to. Reconcile does not send it for
  unlinked pages.
- `page_trashed`: a page was moved to the trash. Arguments: `target`,
  `page`, `event_type` and `instance` (or `None`). Sent before
  `on_notion_trash` is applied.

```python
from django.dispatch import receiver
from notion_sync.signals import page_changed


@receiver(page_changed)
def status_changed(sender, instance, changed_fields, **kwargs):
    if instance is not None and "status" in changed_fields:
        notify_reporter(instance)
```

## Celery

Install the `celery` extra and set `"DISPATCH": "celery"`. Each queued row
is then sent by the `send_outbox_item` task as soon as its transaction
commits. Failed rows wait for their backoff, so also run `drain_outbox` on a
schedule:

```python
CELERY_BEAT_SCHEDULE = {
    "notion-sync-drain": {
        "task": "notion_sync.tasks.drain_outbox",
        "schedule": 60.0,
    },
    # With WEBHOOK_PROCESSING = "stored":
    "notion-sync-webhooks": {
        "task": "notion_sync.tasks.process_webhook_events",
        "schedule": 60.0,
    },
    # Optional: pull recent Notion edits.
    "notion-sync-reconcile": {
        "task": "notion_sync.tasks.reconcile",
        "schedule": 300.0,
    },
}
```

If the task cannot be queued, for example because the broker is down, the row
stays pending and the next drain sends it. Retries are the outbox's, not
Celery's. Celery is imported only when `DISPATCH` is `"celery"` or when
`notion_sync.tasks` is imported. The drain command and the tasks can run at
the same time.

## Admin

With `django.contrib.admin` installed, outbox rows appear under **Notion
sync**. Filter by status, target and operation, and search by row, page,
external or record ID. Rows are read-only. The **Retry selected failed rows**
action sends `failed` and `needs_review` rows again with a fresh set of
attempts. A retried create still looks for a page an earlier attempt may have
made.

Stored webhook events are listed there too, read-only, with their payload and
last error. Superusers also get a page showing the token from Notion's
subscription handshake (see [Setting up webhooks](#setting-up-webhooks)).
`notion_sync_prune` deletes processed events older than `RETENTION_DAYS`
(and never sooner than 48 hours); failed events stay.

## Management commands

| Command | Does | Options |
|---|---|---|
| `notion_sync_check` | Calls Notion and checks each target's token, data source and external ID property, and every mapping against the schema. | `--target NAME` (repeatable) |
| `notion_sync_drain` | Sends the outbox rows that are due, then processes stored webhook events. | `--limit N`: at most N rows, and N events |
| `notion_sync_process_webhooks` | Processes stored webhook events only. | `--limit N` |
| `notion_sync_prune` | Deletes sent and skipped outbox rows, and processed webhook events, older than `RETENTION_DAYS`. Failed ones stay. | `--days N`: override `RETENTION_DAYS` |
| `notion_sync_reconcile` | Applies `In` fields from pages edited within `RECONCILE_LOOKBACK_HOURS`. | `--target NAME` (repeatable), `--since DATE` or `--all` |
| `notion_sync_webhook_token` | Shows the token from Notion's webhook handshake. | |

## Public API

These names are public, and changes to them follow the policy below:

| Import | Names |
|---|---|
| `notion_sync` | `NotionMapping`, `Out`, `In`, `register`, `unregister` |
| `notion_sync.api` | `create_page`, `update_page`, `upsert_page`, `archive_page`, `get_page`, `enqueue_create`, `enqueue_update`, `enqueue_upsert`, `enqueue_archive` |
| `notion_sync.models` | `NotionSyncMixin`, `OutboxItem` (with `Operation` and `Status`), `WebhookEvent` (with `Status`) |
| `notion_sync.files` | `NotionFile` |
| `notion_sync.pages` | `Page` |
| `notion_sync.signals` | `page_synced`, `sync_failed`, `page_changed`, `page_trashed` |
| `notion_sync.exceptions` | `NotionSyncError` and its subclasses |
| `notion_sync.tasks` | the Celery tasks, by their registered names |

The `NOTION_SYNC` setting, the management commands and `notion_sync.urls`
are public too. Each public module lists its names in `__all__`. Every other
module, and every name starting with `_`, is internal and can change in any
release.

The package ships type hints (`py.typed`), and the public modules are fully
typed and checked with mypy.

### Deprecation policy

Before 1.0, a minor release (0.x) can make breaking changes. Each one is
listed under **Breaking** in [CHANGELOG.md](https://github.com/layline-dev/django-notion-sync/blob/main/CHANGELOG.md), with what to change.
From 1.0, a public name is deprecated before it is removed: it keeps working
and raises a `DeprecationWarning` for at least one minor release, and is
removed in a later one.

## Contributing and security

To set up a development environment, run the tests or send a change, see
[CONTRIBUTING.md](https://github.com/layline-dev/django-notion-sync/blob/main/CONTRIBUTING.md). To report a vulnerability privately, see
[SECURITY.md](https://github.com/layline-dev/django-notion-sync/blob/main/SECURITY.md).

## License

MIT. See [LICENSE](https://github.com/layline-dev/django-notion-sync/blob/main/LICENSE).
