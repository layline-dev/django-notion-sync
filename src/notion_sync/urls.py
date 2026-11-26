"""Include in the project's URLconf:

path("notion/webhook/", include("notion_sync.urls"))
"""

from django.urls import path

from notion_sync import views

app_name = "notion_sync"

urlpatterns = [path("", views.webhook, name="webhook")]
