# Security policy

## Supported versions

Until 1.0, only the latest release gets security fixes.

## Reporting a vulnerability

Please don't open a public issue. Report it privately through GitHub instead:
go to the repository's **Security** tab and choose **Report a vulnerability**
([direct link](https://github.com/layline-dev/django-notion-sync/security/advisories/new)).

Include what you found, how to reproduce it, and the versions of the package,
Django and Python you used. You should get a reply within a week. Once a fix
is released, the advisory is published, with credit to you unless you'd rather
not be named.

## Scope

These are the parts most likely to matter:

- **Webhook signatures.** The endpoint checks `X-Notion-Signature`, which is
  an HMAC-SHA256 of the raw request body, against each target's
  `WEBHOOK_VERIFICATION_TOKEN`. Report anything that gets an event past that
  check.
- **Replays and duplicates.** Events older than 48 hours, and event IDs
  already seen, are ignored.
- **Secrets.** Neither the Notion API token nor the webhook verification
  token should ever reach a model, a log line, an exception message or the
  admin. The one exception is the superuser-only handshake page.
- **The admin.** It is read-only apart from the retry action.
