"""Declarative mappings from a Django model to a Notion data source.

    @notion_sync.register(Ticket)
    class TicketMapping(notion_sync.NotionMapping):
        target = "inbox"
        fields = {
            "Name": Out("title"),
            "Reporter": Out(lambda ticket: ticket.user.email),
            "Status": In("status"),
        }

This module is imported by the package's `__init__`, before Django's app
registry is ready, so it must not import models, or modules that import
them (sync), at module level.
"""

import inspect
from collections.abc import Callable, Collection, Iterator
from typing import Any, TypeVar

from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db.models import Field, Model, signals
from django.db.models.fields.files import FieldFile

from notion_sync.conf import CHOICES, Target, get_settings
from notion_sync.files import NotionFile
from notion_sync.schema import Schema
from notion_sync.values import OUTBOUND, READ_ONLY_TYPES

# Fields the mixin adds. They belong to the package, never to a mapping.
MIXIN_FIELDS = frozenset(
    {"notion_page_id", "notion_synced_at", "notion_last_edited_time"}
)

__all__ = ["In", "NotionMapping", "Out", "register", "unregister"]

MappingT = TypeVar("MappingT", bound="NotionMapping")

_registry: dict[type[Model], "NotionMapping"] = {}


class Out:
    """A property written from Django to Notion.

    `source` is an attribute name (dots follow relations, as in
    "user.email"; a method is called) or a callable that takes the instance.
    With `create_only`, the property is written when the page is created and
    left alone afterwards.
    """

    def __init__(
        self, source: str | Callable[[Any], Any], *, create_only: bool = False
    ) -> None:
        if not (callable(source) or (isinstance(source, str) and source)):
            raise ImproperlyConfigured(
                "Out() needs an attribute name or a callable that takes the instance."
            )
        self.source = source
        self.create_only = bool(create_only)

    def __repr__(self) -> str:
        return f"Out({self.source!r})"

    @property
    def field_name(self) -> str | None:
        """The model field this reads directly, or None."""
        if isinstance(self.source, str) and "." not in self.source:
            return self.source
        return None

    def value(self, instance: Model) -> Any:
        """The value to send for this instance."""
        if callable(self.source):
            value = self.source(instance)
        else:
            value = instance
            for part in self.source.split("."):
                if value is None:
                    break
                value = getattr(value, part)
                if inspect.ismethod(value):
                    value = value()
        return plain_value(value)


class In:
    """A property owned by Notion and written back to a model field.

    Webhook events and `notion_sync_reconcile` write the property's value to
    the field. The package never sends it to Notion.
    """

    def __init__(self, field: str) -> None:
        if not isinstance(field, str) or not field:
            raise ImproperlyConfigured("In() needs a model field name.")
        self.field = field

    def __repr__(self) -> str:
        return f"In({self.field!r})"


def plain_value(value: Any) -> Any:
    """Turn model attribute values into values `to_notion` accepts.

    Django stores "no value" in a text field as "", which Notion's select,
    URL, email and phone types reject, so "" becomes None (clear).
    """
    if isinstance(value, FieldFile):
        return NotionFile(value) if value else []
    if isinstance(value, str) and not value:
        return None
    if isinstance(value, list | tuple) and any(
        isinstance(item, FieldFile) for item in value
    ):
        return [NotionFile(item) for item in value if item]
    return value


class NotionMapping:
    """Says which Notion target a model syncs to and how its fields map.

    Subclass it, set `target` and `fields`, and register it with
    `notion_sync.register(Model)`. Options left as None use the project
    default from the NOTION_SYNC setting.
    """

    # Required. Left unset here so a subclass that forgets them fails
    # validation, while type checkers still see a str and a dict.
    target: str
    fields: dict[str, Out | In]
    # Queue a sync after a save that changed an Out field.
    auto_sync: bool | None = None
    # "nothing" or "archive".
    on_delete: str | None = None
    # "nothing", "unlink" or "delete" (inbound).
    on_notion_trash: str | None = None
    # Model fields whose change triggers an auto-sync. None works it out from
    # the Out sources; if any of them is a callable or a dotted path, every
    # save counts as a change unless this is set.
    watch_fields: Collection[str] | None = None

    def __init__(self, model: type[Model]) -> None:
        self.model = model
        self._validate()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} for {self.model._meta.label}>"

    # --- Overridable -------------------------------------------------------

    def get_properties(self, instance: Model, *, creating: bool) -> dict[str, Any]:
        """The values to send, keyed by Notion property name."""
        return {
            name: spec.value(instance)
            for name, spec in self.outbound.items()
            if creating or not spec.create_only
        }

    def get_external_id(self, instance: Model) -> str:
        """The ID stored in the target's EXTERNAL_ID_PROPERTY for this record.

        It must not change for the life of the record. The default is
        "<app label>.<model name>:<primary key>".
        """
        return f"{instance._meta.label_lower}:{instance.pk}"

    # --- Resolved options ---------------------------------------------------

    @property
    def outbound(self) -> dict[str, Out]:
        return {k: v for k, v in self.fields.items() if isinstance(v, Out)}

    @property
    def inbound(self) -> dict[str, In]:
        return {k: v for k, v in self.fields.items() if isinstance(v, In)}

    def get_auto_sync(self) -> bool:
        if self.auto_sync is None:
            return get_settings().auto_sync
        return self.auto_sync

    def get_on_delete(self) -> str:
        return self.on_delete or get_settings().on_delete

    def get_on_notion_trash(self) -> str:
        return self.on_notion_trash or get_settings().on_notion_trash

    def watched(self) -> frozenset[str] | None:
        """Field attnames whose change counts, or None for "any save"."""
        if self.watch_fields is not None:
            return frozenset(self._attname(name) for name in self.watch_fields)
        names = set()
        for spec in self.outbound.values():
            if spec.field_name is None or not self._is_concrete(spec.field_name):
                return None
            names.add(self._attname(spec.field_name))
        return frozenset(names)

    def inbound_attnames(self) -> frozenset[str]:
        return frozenset(self._attname(spec.field) for spec in self.inbound.values())

    # --- Validation -----------------------------------------------------------

    def _concrete_field(self, name: str) -> Field | None:
        try:
            field = self.model._meta.get_field(name)
        except FieldDoesNotExist:
            return None
        return field if isinstance(field, Field) and field.concrete else None

    def _is_concrete(self, name: str) -> bool:
        return self._concrete_field(name) is not None

    def _attname(self, name: str) -> str:
        field = self._concrete_field(name)
        if field is None:
            raise ImproperlyConfigured(
                f"{self._where()}: {name!r} is not a field of {self.model._meta.label}."
            )
        return field.attname

    def _where(self) -> str:
        return f"{type(self).__name__} ({self.model._meta.label})"

    def _validate(self) -> None:
        from notion_sync.models import NotionSyncMixin  # noqa: PLC0415

        where = self._where()
        if not issubclass(self.model, NotionSyncMixin):
            raise ImproperlyConfigured(
                f"{where}: the model must inherit from NotionSyncMixin."
            )
        if not isinstance(getattr(self, "target", None), str) or not self.target:
            raise ImproperlyConfigured(f"{where}: set `target` to a target name.")
        if not isinstance(getattr(self, "fields", None), dict) or not self.fields:
            raise ImproperlyConfigured(
                f"{where}: set `fields` to a dict of Notion property name to "
                "Out() or In()."
            )
        for name, spec in self.fields.items():
            if not isinstance(name, str) or not name:
                raise ImproperlyConfigured(
                    f"{where}: property names must be non-empty strings."
                )
            if not isinstance(spec, Out | In):
                raise ImproperlyConfigured(
                    f'{where}: "{name}" must map to Out() or In(), not {spec!r}.'
                )
            if isinstance(spec, Out) and isinstance(spec.source, str):
                first = spec.source.split(".", 1)[0]
                if not hasattr(self.model, first):
                    raise ImproperlyConfigured(
                        f'{where}: "{name}" reads {spec.source!r}, but '
                        f"{self.model._meta.label} has no attribute {first!r}."
                    )
        outbound_fields = {
            self._attname(spec.field_name)
            for spec in self.outbound.values()
            if spec.field_name and self._is_concrete(spec.field_name)
        }
        for name, spec in self.inbound.items():
            field = self._concrete_field(spec.field)
            if field is None:
                raise ImproperlyConfigured(
                    f'{where}: "{name}" writes to {spec.field!r}, which is not a '
                    f"field of {self.model._meta.label}."
                )
            if field.primary_key or field.name in MIXIN_FIELDS:
                raise ImproperlyConfigured(
                    f'{where}: "{name}" cannot write to {spec.field!r}.'
                )
            if field.attname in outbound_fields:
                raise ImproperlyConfigured(
                    f"{where}: field {spec.field!r} is both sent to Notion and "
                    "written back from it. Each field has one owner."
                )
        if self.auto_sync is not None and not isinstance(self.auto_sync, bool):
            raise ImproperlyConfigured(
                f"{where}: auto_sync must be True, False or None."
            )
        for option, setting in (
            ("on_delete", "ON_DELETE"),
            ("on_notion_trash", "ON_NOTION_TRASH"),
        ):
            value = getattr(self, option)
            if value is not None and value not in CHOICES[setting]:
                raise ImproperlyConfigured(
                    f"{where}: {option} must be one of "
                    f"{', '.join(CHOICES[setting])}; got {value!r}."
                )
        if self.watch_fields is not None:
            if isinstance(self.watch_fields, str):
                raise ImproperlyConfigured(f"{where}: watch_fields must be a list.")
            for field_name in self.watch_fields:
                self._attname(field_name)


# --- Registry ---------------------------------------------------------------


def register(
    *models: type[Model],
) -> Callable[[type[MappingT]], type[MappingT]]:
    """Class decorator: register a `NotionMapping` for one or more models."""
    if not models:
        raise ImproperlyConfigured("register() needs at least one model.")

    def decorator(mapping_class: type[MappingT]) -> type[MappingT]:
        if not (
            isinstance(mapping_class, type) and issubclass(mapping_class, NotionMapping)
        ):
            raise ImproperlyConfigured(
                "register() decorates a subclass of NotionMapping."
            )
        for model in models:
            if model._meta.abstract:
                raise ImproperlyConfigured(
                    f"{model.__name__} is abstract and cannot be registered."
                )
            if model in _registry:
                raise ImproperlyConfigured(
                    f"{model._meta.label} already has a Notion mapping."
                )
            _registry[model] = mapping_class(model)
            connect_signals(model)
        return mapping_class

    return decorator


def unregister(model: type[Model]) -> None:
    """Remove a model's mapping. Mostly useful in tests."""
    if _registry.pop(model, None) is not None:
        disconnect_signals(model)


def get_mapping(model_or_instance: type[Model] | Model) -> NotionMapping | None:
    """The mapping registered for a model or instance, or None."""
    model = (
        model_or_instance
        if isinstance(model_or_instance, type)
        else type(model_or_instance)
    )
    mapping = _registry.get(model)
    concrete = model._meta.concrete_model
    if mapping is None and model._meta.proxy and concrete is not None:
        mapping = _registry.get(concrete)
    return mapping


def require_mapping(model_or_instance: type[Model] | Model) -> NotionMapping:
    mapping = get_mapping(model_or_instance)
    if mapping is None:
        model = getattr(model_or_instance, "_meta", None)
        label = model.label if model else model_or_instance
        raise ImproperlyConfigured(
            f"{label} has no Notion mapping. Register one with notion_sync.register()."
        )
    return mapping


def registered() -> list[NotionMapping]:
    """Every registered mapping, ordered by model label."""
    return sorted(_registry.values(), key=lambda mapping: mapping.model._meta.label)


# --- Signal hooks -------------------------------------------------------------


def signal_uid(kind: str, model: type[Model]) -> str:
    return f"notion_sync:{kind}:{model._meta.label}:{id(model)}"


def connect_signals(model: type[Model]) -> None:
    from notion_sync import sync  # noqa: PLC0415

    signals.post_init.connect(
        sync.remember_values,
        sender=model,
        weak=False,
        dispatch_uid=signal_uid("init", model),
    )
    signals.post_save.connect(
        sync.after_save,
        sender=model,
        weak=False,
        dispatch_uid=signal_uid("save", model),
    )
    signals.post_delete.connect(
        sync.after_delete,
        sender=model,
        weak=False,
        dispatch_uid=signal_uid("delete", model),
    )


def disconnect_signals(model: type[Model]) -> None:
    signals.post_init.disconnect(sender=model, dispatch_uid=signal_uid("init", model))
    signals.post_save.disconnect(sender=model, dispatch_uid=signal_uid("save", model))
    signals.post_delete.disconnect(
        sender=model, dispatch_uid=signal_uid("delete", model)
    )


# --- Checking a mapping against the Notion schema -----------------------------

_TEXT = frozenset(
    {"title", "rich_text", "select", "status", "url", "email", "phone_number"}
)
_NUMBER_FIELDS = (
    "IntegerField",
    "BigIntegerField",
    "SmallIntegerField",
    "PositiveIntegerField",
    "PositiveBigIntegerField",
    "PositiveSmallIntegerField",
    "FloatField",
    "DecimalField",
)
# Notion types a model field's value can be written to. Field types not
# listed (JSON, foreign keys, custom fields) are not checked.
OUTBOUND_TYPES = {
    **dict.fromkeys(
        ("CharField", "TextField", "SlugField", "UUIDField", "GenericIPAddressField"),
        _TEXT,
    ),
    **dict.fromkeys(_NUMBER_FIELDS, frozenset({"number"})),
    "BooleanField": frozenset({"checkbox"}),
    "DateField": frozenset({"date"}),
    "DateTimeField": frozenset({"date"}),
    "FileField": frozenset({"files"}),
    "ImageField": frozenset({"files"}),
}
# Notion types a model field can be filled from.
INBOUND_TYPES = {
    **dict.fromkeys(
        ("CharField", "TextField", "SlugField"),
        _TEXT | {"unique_id"},
    ),
    **dict.fromkeys(_NUMBER_FIELDS, frozenset({"number"})),
    "BooleanField": frozenset({"checkbox"}),
    "DateField": frozenset({"date"}),
    "DateTimeField": frozenset({"date", "created_time", "last_edited_time"}),
}
# Their value type depends on the formula, so any field may take them.
_ANY_INBOUND = frozenset({"formula", "rollup"})


def check_mapping(
    mapping: NotionMapping, target: Target, schema: Schema
) -> Iterator[tuple[bool, str]]:
    """Yield (ok, message) for each property in the mapping."""
    model = mapping.model
    for name, spec in mapping.fields.items():
        prop = schema.get(name)
        if prop is None:
            yield False, f'"{name}" does not exist in the data source.'
            continue
        direction = "from" if isinstance(spec, Out) else "into"
        field = None
        if isinstance(spec, Out):
            if prop.type not in OUTBOUND:
                read_only = prop.type in READ_ONLY_TYPES
                reason = "read-only" if read_only else "not supported"
                yield False, f'"{name}" is a {prop.type} property, which is {reason}.'
                continue
            if name == target.external_id_property:
                yield (
                    False,
                    f'"{name}" is the target\'s external ID property; the package '
                    "writes it, so do not map it.",
                )
                continue
            if spec.field_name:
                field = mapping._concrete_field(spec.field_name)
            allowed = OUTBOUND_TYPES
        else:
            field = mapping._concrete_field(spec.field)
            allowed = INBOUND_TYPES
        if field is not None:
            kind = field.get_internal_type()
            accepted = allowed.get(kind)
            fits = (
                accepted is None
                or prop.type in accepted
                or (isinstance(spec, In) and prop.type in _ANY_INBOUND)
            )
            if not fits:
                yield (
                    False,
                    f'"{name}" is a {prop.type} property; it cannot be filled '
                    f"{direction} {model.__name__}.{field.name} ({kind}).",
                )
                continue
        if isinstance(spec, In):
            where = f"into {spec.field}"
        elif isinstance(spec.source, str):
            where = f"from {spec.source}"
        else:
            where = "from a callable"
        yield True, f'"{name}" ({prop.type}) {where}'
