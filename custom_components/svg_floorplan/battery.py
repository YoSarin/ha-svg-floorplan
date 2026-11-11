"""Battery warning thresholds for floor-plan readings."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Iterable

LOW_BATTERY_THRESHOLD = 15.0
CRITICAL_BATTERY_THRESHOLD = 8.0
MONTHLY_BATTERY_DROP_THRESHOLD = 25.0
BATTERY_TREND_WINDOW = timedelta(days=30)
BATTERY_WARNING_COLOR = "#FFC107"
CRITICAL_BATTERY_WARNING_COLOR = "#F44336"


def battery_needs_warning(
    current_state: str | None,
    historical_states: Iterable[tuple[datetime, str]],
    now: datetime,
) -> bool:
    """Return whether a battery is low or declined more than 25% this month."""
    try:
        current = float(current_state) if current_state is not None else math.nan
    except (TypeError, ValueError):
        return False
    if not math.isfinite(current) or not 0 <= current <= 100:
        return False
    if current < LOW_BATTERY_THRESHOLD:
        return True

    cutoff = now - BATTERY_TREND_WINDOW
    baseline = None
    for changed_at, raw_state in historical_states:
        if changed_at > cutoff:
            continue
        try:
            value = float(raw_state)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and 0 < value <= 100:
            if baseline is None or changed_at > baseline[0]:
                baseline = (changed_at, value)
    if baseline is None:
        return False
    return current < baseline[1] * (1 - MONTHLY_BATTERY_DROP_THRESHOLD / 100)


def battery_warning_color(
    current_state: str | None,
    historical_states: Iterable[tuple[datetime, str]],
    now: datetime,
) -> str | None:
    """Return red for critical charge, orange for other battery warnings."""
    try:
        current = float(current_state) if current_state is not None else math.nan
    except (TypeError, ValueError):
        return None
    if not math.isfinite(current) or not 0 <= current <= 100:
        return None
    if current < CRITICAL_BATTERY_THRESHOLD:
        return CRITICAL_BATTERY_WARNING_COLOR
    if battery_needs_warning(current_state, historical_states, now):
        return BATTERY_WARNING_COLOR
    return None
