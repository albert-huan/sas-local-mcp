"""Independent, bounded LRU caches with single-flight loads across worker threads."""

from collections import OrderedDict
from concurrent.futures import Future
from threading import RLock
from typing import Any, Callable


class ReadCache:
    def __init__(self) -> None:
        self._items: OrderedDict[tuple, tuple[Any, int]] = OrderedDict()
        self._pending: dict[tuple, Future] = {}
        self._lock = RLock()
        self._bytes = 0
        self._generation = 0

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._bytes = 0
            self._generation += 1

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def size_bytes(self) -> int:
        with self._lock:
            return self._bytes

    def peek(self, key: tuple) -> Any:
        """Return a cached value without loading; None on a miss."""
        with self._lock:
            if key not in self._items:
                return None
            self._items.move_to_end(key)
            return self._items[key][0]

    def get_or_load(
        self, key: tuple, loader: Callable[[], Any], *, capacity: int,
        max_bytes: int, weigh: Callable[[Any], int],
    ) -> Any:
        if capacity <= 0:
            return loader()
        with self._lock:
            if key in self._items:
                value, _ = self._items[key]
                self._items.move_to_end(key)
                return value
            generation = self._generation
            pending_key = (generation, key)
            future = self._pending.get(pending_key)
            owner = future is None
            if owner:
                future = self._pending[pending_key] = Future()
        if not owner:
            return future.result()
        try:
            value = loader()
            weight = max(0, weigh(value))
            with self._lock:
                if generation == self._generation:
                    # Keep only the latest file revision for this path/options.
                    for old in list(self._items):
                        if old[0] == key[0] and old[3:] == key[3:] and old != key:
                            self._bytes -= self._items.pop(old)[1]
                    if weight <= max_bytes:
                        self._items[key] = (value, weight)
                        self._bytes += weight
                    while self._items and (len(self._items) > capacity or self._bytes > max_bytes):
                        self._bytes -= self._items.popitem(last=False)[1][1]
            future.set_result(value)
            return value
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                self._pending.pop(pending_key, None)
