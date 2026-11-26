from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from notion_sync import webhooks


@csrf_exempt
@require_POST
def webhook(request):
    """Notion's webhook endpoint. Notion signs each request; there is no login."""
    status = webhooks.receive(
        request.body, request.headers.get(webhooks.SIGNATURE_HEADER)
    )
    return HttpResponse(status=status)
