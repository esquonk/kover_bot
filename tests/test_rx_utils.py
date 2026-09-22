import pytest
import reactivex as rx

from kover_bot.rx_utils import skip_some


def test_skips_exact_requested_number():
    values = []

    rx.from_([1, 2, 3, 4, 5, 6]).pipe(
        skip_some(2, 2, 0, 0, partition=lambda _: "one"),
    ).subscribe(values.append)

    assert values == [3, 6]


def test_tracks_partitions_independently():
    values = []

    rx.from_([("a", 1), ("b", 1), ("a", 2), ("b", 2)]).pipe(
        skip_some(1, 1, 0, 0, partition=lambda item: item[0]),
    ).subscribe(values.append)

    assert values == [("a", 2), ("b", 2)]


def test_can_emit_when_either_count_or_time_limit_is_reached():
    values = []

    rx.from_([1, 2, 3]).pipe(
        skip_some(3, 3, 60, 60, partition=lambda _: "one", require_all=False),
    ).subscribe(values.append)

    assert values == [3]


def test_default_requires_both_count_and_time_limits():
    values = []

    rx.from_([1, 2, 3]).pipe(
        skip_some(1, 1, 60, 60, partition=lambda _: "one"),
    ).subscribe(values.append)

    assert values == []


def test_rejects_invalid_ranges():
    with pytest.raises(ValueError):
        skip_some(2, 1, 0, 0, partition=lambda value: value)

    with pytest.raises(ValueError):
        skip_some(0, 0, 2, 1, partition=lambda value: value)
