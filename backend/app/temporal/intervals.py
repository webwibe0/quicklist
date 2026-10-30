"""Persisted end_time before start_time denotes the following calendar day.

Only the temporal validator may authorize such a crossing from explicit language.
The storage/API shape remains event_date + event_time + end_time (at most 24 hours).
"""
from datetime import datetime, timedelta


def scheduled_interval(day, start, end):
    beginning = datetime.combine(day, start)
    finish = datetime.combine(day, end) + timedelta(days=int(end < start)) if end is not None else beginning
    return beginning, finish


def scheduled_duration(day, start, end):
    beginning, finish = scheduled_interval(day, start, end)
    return finish - beginning
