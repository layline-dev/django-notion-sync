"""Fetches a data source's property schema and caches it in Django's cache."""

from dataclasses import dataclass

from django.core.cache import cache

from notion_sync.conf import get_settings


@dataclass(frozen=True)
class Property:
    name: str
    id: str
    type: str


@dataclass(frozen=True)
class Schema:
    properties: dict
    from_cache: bool = False

    def get(self, name):
        return self.properties.get(name)


def schema_cache_key(target):
    return f"notion_sync:schema:{target.data_source_id}"


def get_schema(target, client, *, refresh=False):
    """Return the target's schema, from the cache unless `refresh` is set."""
    key = schema_cache_key(target)
    seconds = get_settings().schema_cache_seconds
    if not refresh and seconds:
        cached = cache.get(key)
        if cached is not None:
            return Schema(
                {name: Property(name, id_, type_) for name, id_, type_ in cached},
                from_cache=True,
            )
    data_source = client.retrieve_data_source()
    rows = [
        (name, definition.get("id", ""), definition["type"])
        for name, definition in data_source.get("properties", {}).items()
    ]
    if seconds:
        cache.set(key, rows, seconds)
    return Schema({name: Property(name, id_, type_) for name, id_, type_ in rows})


def clear_schema(target):
    cache.delete(schema_cache_key(target))
