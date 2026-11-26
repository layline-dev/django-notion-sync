"""A small in-memory Notion, served through respx.

The SDK runs for real against it. It keeps pages, answers the endpoints the
package uses, and can be told to fail the next matching request, optionally
after applying it (a lost response).
"""

import json
import uuid
from datetime import datetime

import httpx
import respx

API = "https://api.notion.com/v1"

SCHEMA = {
    "Name": "title",
    "Text": "rich_text",
    "Number": "number",
    "Select": "select",
    "Tags": "multi_select",
    "Status": "status",
    "Due": "date",
    "Done": "checkbox",
    "Link": "url",
    "Email": "email",
    "Phone": "phone_number",
    "Owners": "people",
    "Related": "relation",
    "Files": "files",
    "External ID": "rich_text",
    "Score": "formula",
    "Ticket": "unique_id",
    "Created": "created_time",
}

EDITED = "2026-11-22T10:00:00.000Z"


def _instant(value):
    return datetime.fromisoformat(value)


def error_response(status, code, message="Something went wrong.", **kwargs):
    return httpx.Response(
        status,
        json={"object": "error", "status": status, "code": code, "message": message},
        **kwargs,
    )


def as_response_property(prop):
    """Turn a property from a request body into the shape Notion returns."""
    kind = next(iter(prop))
    value = prop[kind]
    if kind in ("title", "rich_text"):
        value = [
            {**item, "plain_text": item["text"]["content"]} for item in value or []
        ]
    return {"id": "prop", "type": kind, kind: value}


class FakeNotion:
    def __init__(self, router):
        self.router = router
        self.schemas = {"ds-inbox": dict(SCHEMA), "ds-plain": dict(SCHEMA)}
        self.pages = {}
        self.uploads = {}
        self.requests = []
        self._faults = []
        # The edit time given to pages created or changed from now on.
        self.now = EDITED

        router.get(url__regex=rf"{API}/data_sources/(?P<ds>[^/]+)$").mock(
            side_effect=self._handler(self._retrieve_data_source)
        )
        router.post(url__regex=rf"{API}/data_sources/(?P<ds>[^/]+)/query$").mock(
            side_effect=self._handler(self._query)
        )
        router.post(f"{API}/pages").mock(side_effect=self._handler(self._create_page))
        router.patch(url__regex=rf"{API}/pages/(?P<page>[^/]+)$").mock(
            side_effect=self._handler(self._update_page)
        )
        router.get(url__regex=rf"{API}/pages/(?P<page>[^/]+)$").mock(
            side_effect=self._handler(self._retrieve_page)
        )
        router.post(f"{API}/file_uploads").mock(
            side_effect=self._handler(self._create_upload)
        )
        router.post(url__regex=rf"{API}/file_uploads/(?P<upload>[^/]+)/send$").mock(
            side_effect=self._handler(self._send_upload)
        )
        router.post(url__regex=rf"{API}/file_uploads/(?P<upload>[^/]+)/complete$").mock(
            side_effect=self._handler(self._complete_upload)
        )

    # --- Test controls ---

    def fail_next(self, method, path_part, result, *, apply=False, times=1):
        """Make the next matching request(s) fail.

        `result` is an httpx.Response or an exception. With `apply`, the
        request takes effect first: the response is what gets lost.
        """
        for _ in range(times):
            self._faults.append((method, path_part, result, apply))

    def count(self, method, path_part):
        return len(self.find(method, path_part))

    def find(self, method, path_part):
        return [
            request
            for request in self.requests
            if request.method == method and path_part in request.url.path
        ]

    def last_json(self, method, path_part):
        return json.loads(self.find(method, path_part)[-1].content)

    def edit(self, page_id, properties=None, *, edited=None, in_trash=None):
        """Change a page as a person in Notion would."""
        page = self.pages[page_id]
        for name, prop in (properties or {}).items():
            page["properties"][name] = as_response_property(prop)
        if in_trash is not None:
            page["in_trash"] = page["archived"] = in_trash
        page["last_edited_time"] = edited or self.now
        return page

    def add_page(self, properties=None, *, data_source="ds-inbox", page_id=None):
        page_id = page_id or str(uuid.uuid4())
        self.pages[page_id] = {
            "object": "page",
            "id": page_id,
            "url": f"https://www.notion.so/{page_id.replace('-', '')}",
            "created_time": self.now,
            "last_edited_time": self.now,
            "archived": False,
            "in_trash": False,
            "parent": {"type": "data_source_id", "data_source_id": data_source},
            "properties": {
                name: as_response_property(prop)
                for name, prop in (properties or {}).items()
            },
        }
        return self.pages[page_id]

    # --- Request handling ---

    def _handler(self, method):
        def handle(request, **params):
            self.requests.append(request)
            for index, (verb, part, result, apply) in enumerate(self._faults):
                if verb == request.method and part in request.url.path:
                    del self._faults[index]
                    if apply:
                        method(request, **params)
                    if isinstance(result, Exception):
                        raise result
                    return result
            return method(request, **params)

        return handle

    def _retrieve_data_source(self, request, ds):
        if ds not in self.schemas:
            return error_response(404, "object_not_found")
        return httpx.Response(
            200,
            json={
                "object": "data_source",
                "id": ds,
                "properties": {
                    name: {"id": f"id-{index}", "name": name, "type": kind, kind: {}}
                    for index, (name, kind) in enumerate(self.schemas[ds].items())
                },
            },
        )

    def _validate(self, ds, properties):
        schema = self.schemas[ds]
        for name, prop in properties.items():
            if name not in schema:
                return error_response(
                    400, "validation_error", f"{name} is not a property that exists."
                )
            if next(iter(prop)) != schema[name]:
                return error_response(
                    400, "validation_error", f"{name} is expected to be {schema[name]}."
                )
        return None

    def _create_page(self, request):
        body = json.loads(request.content)
        ds = body["parent"]["data_source_id"]
        problem = self._validate(ds, body.get("properties", {}))
        if problem:
            return problem
        page = self.add_page(body.get("properties"), data_source=ds)
        return httpx.Response(200, json=page)

    def _update_page(self, request, page):
        if page not in self.pages:
            return error_response(404, "object_not_found")
        body = json.loads(request.content)
        stored = self.pages[page]
        properties = body.get("properties", {})
        problem = self._validate(stored["parent"]["data_source_id"], properties)
        if problem:
            return problem
        for name, prop in properties.items():
            stored["properties"][name] = as_response_property(prop)
        if "in_trash" in body:
            stored["in_trash"] = stored["archived"] = body["in_trash"]
        stored["last_edited_time"] = self.now
        return httpx.Response(200, json=stored)

    def _retrieve_page(self, request, page):
        if page not in self.pages:
            return error_response(404, "object_not_found")
        return httpx.Response(200, json=self.pages[page])

    def _matches(self, page, condition):
        if condition is None:
            return True
        if condition.get("timestamp") == "last_edited_time":
            after = condition["last_edited_time"]["on_or_after"]
            return _instant(page["last_edited_time"]) >= _instant(after)
        name = condition["property"]
        kind = next(key for key in condition if key != "property")
        wanted = condition[kind]["equals"]
        prop = page["properties"].get(name)
        text = "".join(item["plain_text"] for item in prop[kind]) if prop else ""
        return text == wanted

    def _query(self, request, ds):
        body = json.loads(request.content)
        results = [
            page
            for page in self.pages.values()
            if not page["in_trash"]
            and page["parent"]["data_source_id"] == ds
            and self._matches(page, body.get("filter"))
        ]
        for sort in reversed(body.get("sorts", [])):
            results.sort(
                key=lambda page: _instant(page[sort["timestamp"]]),
                reverse=sort.get("direction") == "descending",
            )
        start = int(body.get("start_cursor") or 0)
        size = body.get("page_size", 100)
        chunk = results[start : start + size]
        more = start + size < len(results)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "results": chunk,
                "has_more": more,
                "next_cursor": str(start + size) if more else None,
            },
        )

    def _create_upload(self, request):
        body = json.loads(request.content)
        upload_id = str(uuid.uuid4())
        self.uploads[upload_id] = {"request": body, "parts": [], "completed": False}
        return httpx.Response(
            200, json={"object": "file_upload", "id": upload_id, "status": "pending"}
        )

    def _send_upload(self, request, upload):
        self.uploads[upload]["parts"].append(request.content)
        return httpx.Response(
            200, json={"object": "file_upload", "id": upload, "status": "uploaded"}
        )

    def _complete_upload(self, request, upload):
        self.uploads[upload]["completed"] = True
        return httpx.Response(
            200, json={"object": "file_upload", "id": upload, "status": "uploaded"}
        )


def start():
    """Return a started (router, FakeNotion). Call router.stop() when done."""
    router = respx.mock(assert_all_called=False, assert_all_mocked=True)
    router.start()
    return router, FakeNotion(router)
