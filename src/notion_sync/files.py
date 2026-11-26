"""`NotionFile` and the upload flow.

Bytes go to Notion through its upload API, so no public URL is needed.
"""

import math
import mimetypes
import posixpath
from typing import IO, Any, cast

from django.conf import settings as django_settings
from django.core.files import File
from django.core.files.storage import Storage, storages

from notion_sync.client import NotionClient
from notion_sync.exceptions import NotionValidationError

__all__ = ["NotionFile"]

MEBIBYTE = 1024 * 1024
# Notion takes up to 20 MiB in one request. Above that the file goes in parts
# of 5 to 20 MiB each (the last may be smaller).
SINGLE_PART_LIMIT = 20 * MEBIBYTE
PART_SIZE = 10 * MEBIBYTE


def _storage_alias(storage: Storage) -> str | None:
    """The STORAGES alias for a storage instance, or None."""
    for alias in django_settings.STORAGES:
        if storage == storages[alias]:
            return alias
    return None


class NotionFile:
    """A file to attach to a `files` property.

    `source` is either a storage path (a string, read from the storage named
    by `storage`, an alias in STORAGES) or a Django `File`. A model's file
    field value is a `File` that knows its storage and path, so it can be
    queued; any other `File` can only be sent inline.
    """

    def __init__(
        self,
        source: str | File | IO[bytes],
        *,
        name: str | None = None,
        content_type: str | None = None,
        storage: str | None = None,
    ) -> None:
        self._file: Any = None
        self._storage: Storage | None = None
        self.path: str | None = None
        self.storage_alias: str | None = None

        if isinstance(source, str):
            if not source:
                raise NotionValidationError("NotionFile needs a storage path.")
            self.path = source
            self.storage_alias = storage or "default"
            if self.storage_alias not in django_settings.STORAGES:
                raise NotionValidationError(
                    f"NotionFile: no storage named {self.storage_alias!r} in STORAGES."
                )
        elif hasattr(source, "storage") and getattr(source, "name", None):
            if storage is not None:
                raise NotionValidationError(
                    "NotionFile: `storage` only applies to a storage path."
                )
            self.path = source.name
            self._storage = source.storage
            self.storage_alias = _storage_alias(source.storage)
        elif hasattr(source, "read"):
            if storage is not None:
                raise NotionValidationError(
                    "NotionFile: `storage` only applies to a storage path."
                )
            self._file = source
        else:
            raise NotionValidationError(
                "NotionFile needs a storage path or a Django File; "
                f"got {type(source).__name__}."
            )

        fallback = self.path or getattr(source, "name", None) or ""
        self.name = name or posixpath.basename(str(fallback).replace("\\", "/"))
        if not self.name:
            raise NotionValidationError(
                "NotionFile cannot work out a file name; pass name=."
            )
        self.content_type = content_type or mimetypes.guess_type(self.name)[0]

    def __repr__(self) -> str:
        return f"NotionFile({self.path or self.name!r})"

    @property
    def storage(self) -> Storage | None:
        """The storage the file is read from, or None for an in-memory file."""
        if self._storage is None and self.storage_alias is not None:
            self._storage = storages[self.storage_alias]
        return self._storage

    def read(self) -> bytes:
        """Return the file's bytes."""
        if self._file is not None:
            if hasattr(self._file, "seek"):
                self._file.seek(0)
            data = self._file.read()
            return data.encode() if isinstance(data, str) else data
        # With no in-memory file, __init__ set both a path and a storage.
        storage = cast(Storage, self.storage)
        path = cast(str, self.path)
        try:
            with storage.open(path, "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            raise NotionValidationError(
                f"File {self.path!r} was not found in storage."
            ) from None

    def to_snapshot(self) -> dict[str, str | None]:
        """A JSON-safe reference for the outbox: storage alias and path only."""
        if self.path is None or self.storage_alias is None:
            raise NotionValidationError(
                "A queued NotionFile must come from a storage in STORAGES. "
                "Save the file first and pass its storage path or model field."
            )
        return {
            "path": self.path,
            "storage": self.storage_alias,
            "name": self.name,
            "content_type": self.content_type,
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> "NotionFile":
        """Rebuild a `NotionFile` from `to_snapshot()` output."""
        return cls(
            data["path"],
            storage=data["storage"],
            name=data.get("name"),
            content_type=data.get("content_type"),
        )


def upload_file(client: NotionClient, notion_file: NotionFile) -> str:
    """Upload the file's bytes to Notion and return the file upload ID."""
    content = notion_file.read()
    name = notion_file.name
    content_type = notion_file.content_type

    if len(content) <= SINGLE_PART_LIMIT:
        upload = client.create_file_upload(filename=name, content_type=content_type)
        client.send_file_upload(
            upload["id"], filename=name, content=content, content_type=content_type
        )
        return upload["id"]

    parts = math.ceil(len(content) / PART_SIZE)
    upload = client.create_file_upload(
        filename=name, content_type=content_type, number_of_parts=parts
    )
    for index in range(parts):
        client.send_file_upload(
            upload["id"],
            filename=name,
            content=content[index * PART_SIZE : (index + 1) * PART_SIZE],
            content_type=content_type,
            part_number=index + 1,
        )
    client.complete_file_upload(upload["id"])
    return upload["id"]
