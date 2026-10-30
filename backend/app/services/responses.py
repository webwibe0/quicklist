from uuid import UUID

from app.schemas.commands import (
    CreateSeriesCommand,
    EventResponse,
    Intent,
    MessageResponse,
    UpdateSeriesCommand,
    creation_commands,
)

QUESTIONS = {
    "referent": "Кому именно позвонить?",
    "event_reference": "Какое именно событие изменить? Уточните содержание события.",
    "event_type": "Это задача, напоминание или встреча?",
    "title": "Что нужно запланировать? Укажите название или содержание.",
    "date": "На какую дату?",
    "time": "На какое время? Укажите ЧЧ:ММ.",
    "end_time": "Во сколько окончание? Укажите ЧЧ:ММ.",
    "period": "За какой период показать события?",
    "recurrence": "Как должно повторяться событие? Например: «каждый день», «по будням» или «каждый понедельник».",
    "recurrence_scope": "Применить действие только к этому событию или ко всей серии? "
                        "Ответьте «только это событие» или «всю серию».",
    "search_query": "Что искать в истории?",
}


def next_day_note(event):
    return " следующего дня" if event.end_time and event.event_time and event.end_time < event.event_time else ""


class ResponseBuilder:
    def __init__(self, limit: int):
        self.limit = limit

    def past_update_confirmation(self, conversation_id, command):
        series = isinstance(command, UpdateSeriesCommand)
        if series:
            command = next(item for item in command.updates if item.event_time is not None)
        return MessageResponse(
            conversation_id=conversation_id, action=Intent.UPDATE_EVENT,
            message=(f"Новое время — {command.event_date:%d.%m.%Y} в "
                     f"{command.event_time:%H:%M} (Europe/Moscow) — уже прошло. "
                     + ("Всё равно изменить всю серию?" if series else "Всё равно перенести событие?")
                     + " Ответьте «да» или «нет»."),
            requires_input=True, missing_fields=["confirmation"],
        )
    def update_conflict_confirmation(self, conversation_id, event, *, series=False):
        schedule = ""
        if event.event_time:
            schedule += f"{event.event_time:%H:%M}"
        if event.end_time:
            schedule += f"–{event.end_time:%H:%M}" + next_day_note(event)

        return MessageResponse(
            conversation_id=conversation_id,
            action=Intent.UPDATE_EVENT,
            message=(
                f"На это время уже запланировано «{event.title}»"
                + (f" на {event.event_date:%d.%m.%Y}" if series else "")
                + (f" — {schedule}" if schedule else "")
                + (". Всё равно изменить всю серию?" if series else ". Всё равно перенести встречу?")
                + " Ответьте «да» или «нет»."
            ),
            requires_input=True,
            missing_fields=["confirmation"],
        )

    def delete_confirmation(self, conversation_id, event, *, series=False):
        schedule = (f" на {event.event_date:%d.%m.%Y}" if event.event_date else "")
        schedule += f" в {event.event_time:%H:%M}" if event.event_time else ""
        schedule += f"–{event.end_time:%H:%M}" + next_day_note(event) if event.end_time else ""
        return MessageResponse(
            conversation_id=conversation_id, action=Intent.DELETE_EVENT,
            message=(f"Удалить всю серию «{event.title}»? Ответьте «да» или «нет»." if series
                     else f"Удалить «{event.title}»{schedule}? Ответьте «да» или «нет»."),
            requires_input=True, missing_fields=["confirmation"],
        )

    def not_found(self, conversation_id, intent):
        message = "Подходящая запись не найдена или уже недоступна для этого действия."
        return MessageResponse(conversation_id=conversation_id, action=intent, message=message)

    def candidates(self, conversation_id, intent, events):
        visible = events[:self.limit]
        lines = []
        for index, event in enumerate(visible, 1):
            schedule = f", {event.event_date:%d.%m.%Y}" if event.event_date else ""
            schedule += f" в {event.event_time:%H:%M}" if event.event_time else ""
            schedule += f"–{event.end_time:%H:%M}" + next_day_note(event) if event.end_time else ""
            lines.append(f"{index}. {event.title}{schedule}")
        return MessageResponse(
            conversation_id=conversation_id, action=intent, requires_input=True,
            missing_fields=["event_reference"],
            message=("Найдена похожая запись. Проверьте название и выберите номер:\n" if len(visible) == 1
                     else "Найдено несколько записей. Уточните номер или содержание:\n") + "\n".join(lines),
            events=[EventResponse.model_validate(event) for event in visible],
        )

    def create_confirmation(self, conversation_id, pending):
        commands = creation_commands(pending.confirmation)
        schedule = "; ".join(
            (f"«{item.title}» — " if len(commands) > 1 else "")
            + (f"{item.event_date:%d.%m.%Y}" if item.event_date else "без даты")
            + (f" в {item.event_time:%H:%M}" if item.event_time else "")
            + (f" до {item.end_time:%H:%M}" + next_day_note(item) if item.end_time else "")
            for item in commands
        )
        if pending.confirmation_kind == "past":
            alternatives = creation_commands(pending.alternative)
            alternative = "; ".join(
                f"{item.event_date:%d.%m.%Y} в {item.event_time:%H:%M}" for item in alternatives
            )
            message = (f"Время {schedule} (Europe/Moscow) уже прошло. "
                       "Всё равно создать? Ответьте «да», чтобы сохранить исходное время, "
                       f"или «завтра», чтобы создать на {alternative}. «Нет» — отмена.")
        elif pending.confirmation_kind == "duplicate":
            if isinstance(pending.confirmation, CreateSeriesCommand):
                message = (f"В серии «{commands[0].title}» есть точный дубль. "
                           f"Создать всю серию ещё раз? Событий: {len(commands)}. Ответьте «да» или «нет».")
            elif len(commands) > 1:
                message = (f"Такие напоминания уже существуют: {schedule}. "
                           "Создать ещё по одному? Ответьте «да» или «нет».")
            else:
                message = (f"Такое событие уже есть: «{commands[0].title}», {schedule}. "
                           "Создать ещё одно? Ответьте «да» или «нет».")
        elif pending.confirmation_kind == "task_conversion":
            message = (f"Понял, точное время не важно. Создать задачу «{commands[0].title}», "
                       f"{schedule}, без времени? Ответьте «да» или «нет». "
                       "Для напоминания укажите точное время.")
        elif pending.confirmation_kind == "conflict":
            event = pending.confirmation_event
            schedule = ""
            if event.event_time:
                schedule += f"{event.event_time:%H:%M}"
            if event.end_time:
                schedule += f"–{event.end_time:%H:%M}" + next_day_note(event)

            message = (
                f"На это время уже запланировано «{event.title}»"
                + (f" на {event.event_date:%d.%m.%Y}" if isinstance(pending.confirmation, CreateSeriesCommand) else "")
                + (f" — {schedule}" if schedule else "")
                + (". Всё равно создать всю серию?" if isinstance(pending.confirmation, CreateSeriesCommand)
                   else ". Всё равно создать встречу?")
                + " Ответьте «да» или «нет»."
            )
        elif pending.confirmation_kind == "unscheduled_meeting":
            message = (f"Сохранить встречу «{commands[0].title}», {schedule}, без точного времени? "
                       "Время останется неопределённым. Ответьте «да» или «нет».")
        else:
            count = "два" if len(commands) == 2 else str(len(commands))
            message = (f"Создать {count} напоминания: {schedule}? "
                       "Ответьте «да» или «нет».")
        return MessageResponse(
            conversation_id=conversation_id, action=Intent.CREATE_EVENT, message=message,
            requires_input=True, missing_fields=["confirmation"],
        )

    def clarification(self, conversation_id, intent, missing, issues):
        questions = [issues.get(field, QUESTIONS[field]) for field in missing]
        return MessageResponse(
            conversation_id=conversation_id, action=intent,
            message=" ".join(questions), requires_input=True, missing_fields=missing,
        )

    def unknown(self, conversation_id):
        return MessageResponse(
            conversation_id=conversation_id, action=Intent.UNKNOWN,
            message="Я могу создать и изменить событие, показать планы, найти записи, "
                    "удалить событие или отметить задачу выполненной. "
                    "Например: «Что у меня завтра?»",
        )

    def success(self, conversation_id: UUID, intent: Intent, events):
        total_count = len(events)
        series_id = getattr(events[0], "recurrence_series_id", None) if events else None
        series = series_id is not None and all(event.recurrence_series_id == series_id for event in events)
        materialized_end = max((event.event_date for event in events if event.event_date), default=None)
        truncated = total_count > self.limit
        events = events[:self.limit]
        if not events and intent in (Intent.DELETE_EVENT, Intent.COMPLETE_EVENT, Intent.CREATE_EVENT, Intent.UPDATE_EVENT):
            return self.not_found(conversation_id, intent)
        if intent in (Intent.DELETE_EVENT, Intent.COMPLETE_EVENT):
            verb = ("Удалено" if intent == Intent.DELETE_EVENT else
                    "Встреча состоялась" if events[0].type == "meeting" else "Выполнено")
            message = f"{verb}: {events[0].title}."
            if series and intent == Intent.DELETE_EVENT and total_count > 1:
                message = f"Удалена серия: {events[0].title}. Событий: {total_count}."
        elif intent in (Intent.CREATE_EVENT, Intent.UPDATE_EVENT):
            event = events[0]
            schedule = ""
            if event.event_date:
                schedule += f" на {event.event_date.strftime('%d.%m.%Y')}"
            if event.event_time:
                schedule += f" в {event.event_time.strftime('%H:%M')}"
            if event.end_time:
                schedule += f" до {event.end_time:%H:%M}" + next_day_note(event)
            if event.type == "meeting" and event.event_time is None:
                schedule += ", время не определено"
            verb = "Обновлено" if intent == Intent.UPDATE_EVENT else "Сохранено"
            message = f"Готово. {verb}: {event.title}{schedule}."
            if intent == Intent.UPDATE_EVENT and series and total_count > 1:
                message = f"Готово. Обновлена серия: {event.title}. Событий: {total_count}."
            if intent == Intent.CREATE_EVENT and (total_count > 1 or series):
                if series:
                    message = (
                        f"Готово. Создана серия: {event.title}. "
                        f"Событий: {total_count}."
                    )
                    rule = getattr(event, "recurrence_rule", "") or ""
                    if materialized_end and "COUNT=" not in rule and "UNTIL=" not in rule:
                        message += (
                            f" Повторения сохранены по {materialized_end:%d.%m.%Y} включительно. "
                            "Серия не продлевается автоматически; для другого периода укажите дату окончания "
                            "или количество повторений."
                        )
                else:
                    message = f"Готово. Создано напоминаний: {total_count}."
        elif not events:
            message = "За этот период событий нет." if intent == Intent.LIST_EVENTS else "Подходящих записей не найдено."
        else:
            message = f"Найдено событий: {len(events)}."
        if truncated:
            message += f" Показаны первые {self.limit}; уточните запрос для остальных."
        return MessageResponse(
            conversation_id=conversation_id, action=intent, message=message,
            events=[EventResponse.model_validate(event) for event in events],
        )
