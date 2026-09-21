import random
from collections.abc import Callable, Hashable
from time import time
from typing import Any

import reactivex as rx


def skip_some(
    min_skip: int,
    max_skip: int,
    min_time: float,
    max_time: float,
    partition: Callable[[Any], Hashable],
):
    """
    Skip between min_skip and max_skip values, and between min_time and max_time seconds,
    whichever comes last, then reset the counters.
    """

    if min_skip < 0 or min_skip > max_skip:
        raise ValueError("min_skip must be non-negative and no greater than max_skip")
    if min_time < 0 or min_time > max_time:
        raise ValueError("min_time must be non-negative and no greater than max_time")

    def _skip_some(source):
        def subscribe(observer, scheduler=None):
            stats: dict[Hashable, dict[str, Any]] = {}

            def _reset_stats(partition_key):
                if partition_key not in stats:
                    stats[partition_key] = {}

                stats[partition_key].update(
                    {
                        "skipped": 0,
                        "last_message": time(),
                        "to_skip": random.randint(min_skip, max_skip),
                        "to_wait": random.random() * (max_time - min_time) + min_time,
                    }
                )

            def on_next(value):
                partition_key = partition(value)
                if partition_key not in stats:
                    _reset_stats(partition_key)

                state = stats[partition_key]
                if state["skipped"] < state["to_skip"]:
                    state["skipped"] += 1
                elif state["last_message"] + state["to_wait"] <= time():
                    observer.on_next(value)
                    _reset_stats(partition_key)

            return source.subscribe(
                on_next, observer.on_error, observer.on_completed, scheduler=scheduler
            )

        return rx.create(subscribe)

    return _skip_some
