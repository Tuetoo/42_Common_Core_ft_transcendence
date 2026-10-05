from typing import TypeVar
from prometheus_client import REGISTRY, Counter, Gauge, Histogram

_C = TypeVar("_C", Counter, Gauge, Histogram)


def get_or_create(cls: type[_C], name: str, doc: str,
                  labels: tuple[str, ...] = ()) -> _C:
    existing = REGISTRY._names_to_collectors.get(name)
    if isinstance(existing, cls):
        return existing
    return cls(name, doc, labels)
