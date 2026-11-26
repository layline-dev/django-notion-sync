from django.core.management.base import BaseCommand, CommandError

from notion_sync import webhooks


class Command(BaseCommand):
    help = (
        "Show the verification token Notion sent when the webhook subscription "
        "was created. Tokens are kept in the cache for 15 minutes."
    )

    def handle(self, *args, **options):
        tokens = webhooks.recent_verification_tokens()
        if not tokens:
            raise CommandError(
                "No verification token was received in the last 15 minutes. In "
                "Notion, open the integration's Webhooks tab and click "
                '"Resend token", then run this again.'
            )
        if len(tokens) > 1:
            self.stderr.write(
                self.style.WARNING(
                    f"{len(tokens)} tokens arrived in the last 15 minutes. The "
                    "handshake request is not signed, so anyone who knows the URL "
                    "can post one. Only the token Notion accepts in its Verify "
                    "dialog is real; use that one."
                )
            )
        for token, received in tokens:
            label = received.strftime("%H:%M:%S UTC")
            self.stdout.write(
                f"{token}  (received {label})" if len(tokens) > 1 else token
            )
