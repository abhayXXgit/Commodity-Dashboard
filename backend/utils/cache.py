"""
Tiny TTL memo cache for derived read models.

The BOM roll-up walks every transformer, every BOM line and the latest price of
every product. A single dashboard screen asks for it six times through six
different endpoints, so without a cache one screen costs six full rebuilds.

The underlying data only changes on an import, a refresh, or a BOM/quote edit,
so a short TTL plus an explicit `invalidate()` on every write path is both
correct and simple. Nothing here is a substitute for the database — it is a
read-through memo with a bounded lifetime.
"""
from __future__ import annotations

import functools
import threading
import time
from typing import Any, Callable

_store: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()
_generation = 0

DEFAULT_TTL = 45.0


def invalidate(reason: str = "") -> None:
    """Drop everything. Call from any path that writes price/BOM/quote data."""
    global _generation
    with _lock:
        _store.clear()
        _generation += 1


def generation() -> int:
    return _generation


def memo(ttl: float = DEFAULT_TTL, key: Callable[..., str] | None = None):
    """
    Cache a function's result for `ttl` seconds.

    The cache key includes the invalidation generation, so `invalidate()` makes
    every previously stored entry unreachable without having to walk the store.
    The first positional argument is assumed to be a DB session and is excluded
    from the key.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                extra = key(*args, **kwargs) if key else _default_key(args, kwargs)
            except Exception:                               # noqa: BLE001
                return fn(*args, **kwargs)                  # unkeyable -> don't cache
            k = f"{_generation}:{fn.__module__}.{fn.__qualname__}:{extra}"
            now = time.time()
            with _lock:
                hit = _store.get(k)
                if hit and hit[0] > now:
                    return hit[1]
            value = fn(*args, **kwargs)
            with _lock:
                _store[k] = (now + ttl, value)
                if len(_store) > 512:                       # bound the store
                    for stale in [kk for kk, (exp, _) in _store.items() if exp <= now]:
                        _store.pop(stale, None)
            return value
        wrapper.cache_clear = invalidate                    # type: ignore[attr-defined]
        return wrapper
    return deco


def _default_key(args: tuple, kwargs: dict) -> str:
    """Skip arg 0 (the session); everything else must be hashable/serialisable."""
    parts = [repr(a) for a in args[1:]]
    parts += [f"{k}={v!r}" for k, v in sorted(kwargs.items())]
    return "|".join(parts)


def stats() -> dict[str, Any]:
    with _lock:
        return {"entries": len(_store), "generation": _generation}
