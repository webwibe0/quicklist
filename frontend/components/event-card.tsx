import { GoogleEventSync } from "./google-calendar";
import type { Event, EventAction } from "@/types/assistant";
import { Icon, type IconName } from "./icons";
import { dayLabel, dateKey, eventDate, eventTime } from "@/lib/events";

const types: Record<Event["type"], {name: string; icon: IconName}> = {
  task: {name: "Задача", icon: "task"}, reminder: {name: "Напоминание", icon: "bell"},
  meeting: {name: "Встреча", icon: "calendar"},
};

export function EventCard({event, onAction, disabled = false}: {
  event: Event; onAction?: (action: EventAction, event: Event) => void; disabled?: boolean;
}) {
  if (event.status === "deleted") return null;
  const type = types[event.type];
  const time = eventTime(event);
  const relativeDay = dayLabel(event.event_date, dateKey());
  return <article className={`event-card type-${event.type} event-${event.status}`}>
    <div className="event-content">
    <div className="event-heading"><span className="event-type"><Icon name={type.icon} />{type.name}</span>
      {event.status === "completed" && <span className="event-status" role="status">{event.type === "meeting" ? "Состоялась" : "Выполнено"}</span>}
      {event.status === "cancelled" && <span className="event-status">Отменено</span>}
    </div>
    {time && <p className="event-time">{time}{event.end_time && event.event_time && event.end_time < event.event_time
      && <small className="event-time-note"> · +1 день</small>}</p>}
    <h3 className="event-title">{event.title}</h3>
    <p className="event-schedule"><Icon name="calendar" /><span>{event.event_date
      ? <time dateTime={event.event_date}>{["Сегодня", "Завтра"].includes(relativeDay)
        ? `${relativeDay} · ${eventDate(event.event_date)}` : eventDate(event.event_date)}</time> : "Без даты"}
      {!time && <span className="event-time-unspecified"> · Время не указано</span>}</span></p>
    {event.type_inferred && <p className="inferred-note">Тип определён по исходным данным</p>}
    <GoogleEventSync event={event} disabled={disabled} />
    </div>
    {onAction && <div className="event-actions">
      {event.status === "pending" && <button type="button" disabled={disabled}
        onClick={() => onAction("COMPLETE_EVENT", event)} className="button button-small button-soft">
        <Icon name="check" />{event.type === "meeting" ? "Состоялась" : "Выполнено"}</button>}
      <button type="button" disabled={disabled}
        onClick={() => onAction("DELETE_EVENT", event)} className="button button-small button-quiet delete-action">
        <Icon name="trash" />Удалить</button>
    </div>}
  </article>;
}
