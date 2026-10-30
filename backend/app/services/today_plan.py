"""Read-only daily projection of stored occurrences, never an LLM or recurrence writer."""
from zoneinfo import ZoneInfo

from app.temporal.intervals import scheduled_interval


def build_today_plan(repository, timezone, now):
    if now.tzinfo is None:
        raise ValueError('A timezone-aware planner clock is required')
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    today = local.date()
    upcoming, without_time, overdue_today, overdue_earlier, ongoing = [], [], [], [], []
    undated_count = 0
    # ponytail: one bounded-by-account read for the current small prototype;
    # paginate the older backlog before deploying for very large accounts.
    for event in repository.daily_candidates(today):
        if event.event_date is None:
            undated_count += 1
            continue
        if event.event_time is None:
            (without_time if event.event_date == today else overdue_earlier).append(event)
            continue
        start, end = scheduled_interval(event.event_date, event.event_time, event.end_time)
        start, end = start.replace(tzinfo=zone), end.replace(tzinfo=zone)
        if end.date() < today:
            overdue_earlier.append(event)
        elif event.end_time is not None and start <= local < end:
            upcoming.append(event)
            ongoing.append(event.id)
        elif start >= local:
            upcoming.append(event)
        else:
            overdue_today.append(event)
    return {'today': today, 'timezone': timezone, 'as_of': local,
            'total_today': len(upcoming) + len(without_time) + len(overdue_today),
            'upcoming': upcoming, 'without_time': without_time, 'overdue_today': overdue_today,
            'overdue_earlier': overdue_earlier, 'ongoing_ids': ongoing, 'undated_count': undated_count}
