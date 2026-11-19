"""Transaction-scoped record cache.

One cache is shared by every :class:`~tessera.orm.environment.Environment` of the same
transaction (``with_context`` / ``with_user`` siblings). Values are stored in *cache* shape
(see :mod:`tessera.orm.fields`). Translatable fields are keyed by language and
company-dependent fields by company so that different environments never see each
other's values.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tessera.orm.fields import Field

CacheKey = tuple[str, str, Any]  # (model, field, qualifier)

_MISSING: Any = object()


class Cache:
    def __init__(self) -> None:
        self._data: dict[CacheKey, dict[int, Any]] = defaultdict(dict)
        # fields whose cached value is being computed (to detect cycles)
        self.computing: set[tuple[str, str]] = set()

    @staticmethod
    def key(field: Field, qualifier: Any = None) -> CacheKey:
        return (field.model_name, field.name, qualifier)

    def get(self, field: Field, record_id: int, qualifier: Any = None, default: Any = _MISSING) -> Any:
        values = self._data.get(self.key(field, qualifier))
        if values is None or record_id not in values:
            if default is _MISSING:
                raise KeyError((field.model_name, field.name, record_id))
            return default
        return values[record_id]

    def contains(self, field: Field, record_id: int, qualifier: Any = None) -> bool:
        values = self._data.get(self.key(field, qualifier))
        return values is not None and record_id in values

    def missing_ids(self, field: Field, ids: Iterable[int], qualifier: Any = None) -> list[int]:
        values = self._data.get(self.key(field, qualifier), {})
        return [i for i in ids if i not in values]

    def set(self, field: Field, record_id: int, value: Any, qualifier: Any = None) -> None:
        self._data[self.key(field, qualifier)][record_id] = value

    def set_many(self, field: Field, values: Mapping[int, Any], qualifier: Any = None) -> None:
        self._data[self.key(field, qualifier)].update(values)

    def invalidate(
        self,
        model_name: str | None = None,
        field_names: Iterable[str] | None = None,
        ids: Iterable[int] | None = None,
    ) -> None:
        """Drop cached values. With no arguments the whole cache is cleared."""
        if model_name is None:
            self._data.clear()
            return
        names = set(field_names) if field_names is not None else None
        id_set = set(ids) if ids is not None else None
        for key in list(self._data):
            if key[0] != model_name or (names is not None and key[1] not in names):
                continue
            if id_set is None:
                del self._data[key]
            else:
                bucket = self._data[key]
                for i in id_set:
                    bucket.pop(i, None)

    def invalidate_field(self, field: Field, ids: Iterable[int] | None = None) -> None:
        self.invalidate(field.model_name, [field.name], ids)

    def clear(self) -> None:
        self._data.clear()
        self.computing.clear()

    def __len__(self) -> int:
        return sum(len(v) for v in self._data.values())
