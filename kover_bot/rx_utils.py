import random
from time import time
from typing import Any, Callable, Hashable

import reactivex as rx


def skip_some(min_skip: int, max_skip: int, min_time: float, max_time: float,
              partition: Callable[[Any], Hashable]):
    """
    Skip between min_skip and max_skip values, and between min_time and max_time seconds,
    whichever comes last, then reset the counters.
    """

    def _skip_some(source):
        def subscribe(observer, scheduler=None):
            stats: dict[Hashable, dict[str, Any]] = {}

            def _reset_stats(partition_key):
                if partition_key not in stats:
                    stats[partition_key] = {}

                stats[partition_key].update({
                    'skipped': 0,
                    'last_message': time(),
                    'to_skip': random.randint(min_skip, max_skip),
                    'to_wait': random.random() * (max_time - min_time) + min_time,
                })

            def on_next(value):
                partition_key = partition(value)
                if partition_key not in stats:
                    _reset_stats(partition_key)

                elif (
                        stats[partition_key]['skipped'] >= stats[partition_key]['to_skip'] and
                        stats[partition_key]['last_message'] + stats[partition_key]['to_wait']
                        <= time()
                ):
                    observer.on_next(value)
                    _reset_stats(partition_key)
                else:
                    stats[partition_key]['skipped'] += 1

            return source.subscribe(
                on_next,
                observer.on_error,
                observer.on_completed,
                scheduler=scheduler
            )

        return rx.create(subscribe)

    return _skip_some
