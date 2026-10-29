#!/usr/bin/env python3
"""Операции над временными интервалами (1 копия вместо дублей в main.py /
preparationToMeaningfulNeyroRequest.py).

Все интервалы — полуоткрытые [start, end) в epoch-секундах.
"""

from __future__ import annotations

from typing import List, Tuple

Interval = Tuple[float, float]


def merge_intervals(intervals: List[Interval]) -> List[Interval]:
    """Сливает пересекающиеся/касающиеся интервалы."""
    if not intervals:
        return []
    s = sorted(intervals, key=lambda x: x[0])
    merged: List[Interval] = [s[0]]
    for cur_s, cur_e in s[1:]:
        last_s, last_e = merged[-1]
        if cur_s <= last_e:
            merged[-1] = (last_s, max(last_e, cur_e))
        else:
            merged.append((cur_s, cur_e))
    return merged


def subtract_intervals(start: float, end: float,
                       covered: List[Interval]) -> List[Interval]:
    """Части [start, end), не покрытые ни одним интервалом covered.

    covered должен быть отсортирован и без перекрытий.
    """
    result: List[Interval] = []
    cur = start
    for cs, ce in sorted(covered, key=lambda x: x[0]):
        if ce <= cur:
            continue
        if cs >= end:
            break
        if cs > cur:
            result.append((cur, min(cs, end)))
        cur = max(cur, ce)
    if cur < end:
        result.append((cur, end))
    return result


def merge_into_covered(covered: List[Interval],
                       new_start: float, new_end: float) -> List[Interval]:
    """Добавляет интервал в covered и сливает перекрытия."""
    merged: List[Interval] = []
    ns, ne = new_start, new_end
    added = False
    for cs, ce in covered:
        if ce < ns:
            merged.append((cs, ce))
        elif cs > ne:
            if not added:
                merged.append((ns, ne))
                added = True
            merged.append((cs, ce))
        else:
            ns = min(ns, cs)
            ne = max(ne, ce)
    if not added:
        merged.append((ns, ne))
    return merged
