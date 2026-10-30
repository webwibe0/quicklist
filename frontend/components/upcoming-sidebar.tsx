import type { Event } from "@/types/assistant";
import { dayLabel, eventDate, eventTime } from "@/lib/events";
import { Icon, type IconName } from "./icons";

const typeNames = {task: "Задача", reminder: "Напоминание", meeting: "Встреча"};

export function upcomingIcon(event: Pick<Event, "type" | "title">): IconName {
  const title = event.title.toLocaleLowerCase("ru").replaceAll("ё", "е");
  if (/zoom|зум|онлайн|online|видео|google\s*meet|teams/.test(title)) return "video";
  if (/позвон|созвон|звонок|телефон/.test(title)) return "phone";
  if (event.type === "reminder") return "bell";
  if (event.type === "task") return "task";
  if (event.type === "meeting") return /(?:^|\s)с\s|друз|команд|коллег/.test(title) ? "people" : "calendar";
  return "clock";
}

export function PlannerEvents({events, today, ongoingIds = [], nearest = false}: {
  events: Event[]; today: string; ongoingIds?: string[]; nearest?: boolean;
}) {
  return <ol className={`timeline-list${nearest ? ' planner-nearest' : ''}`}>{events.map((event, index) => <li key={event.id}>
      {event.event_date !== today && (index === 0 || events[index - 1].event_date !== event.event_date) &&
        <h3 className="upcoming-day">{dayLabel(event.event_date, today)}</h3>}
      <div className={`timeline-event timeline-${event.type}`}>
        <span className={`timeline-time${!event.event_time ? " timeline-unscheduled" : ""}`} aria-label={eventTime(event) || "Время не задано"}>
          {event.event_time ? <>{event.event_time.slice(0, 5)}{event.end_time && <small>–{event.end_time.slice(0, 5)}</small>}</> : "Без времени"}
        </span>
        <span className="timeline-marker" aria-hidden="true" />
        <div className="timeline-content"><p>{event.title}</p><span>{typeNames[event.type]}{ongoingIds.includes(event.id) && ' · Идёт сейчас'}{event.recurrence_series_id && ' · Повторяется'}</span></div>
        <Icon name={upcomingIcon(event)} className="timeline-icon" data-event-icon={upcomingIcon(event)} />
      </div>
    </li>)}</ol>;
}

export function UpcomingSidebar({events, today, total = events.length, withoutTime = [], overdueToday = [], overdueEarlier = [],
  ongoingIds = [], undatedCount = 0, loading = false, error = '', asOf = '', disabled, onShowPlans}: {
  events: Event[]; today: string; total?: number; withoutTime?: Event[]; overdueToday?: Event[]; overdueEarlier?: Event[];
  ongoingIds?: string[]; undatedCount?: number; loading?: boolean; error?: string; asOf?: string;
  disabled: boolean; onShowPlans: () => void;
}) {
  const visible = events.slice(0,5);
  const badge = loading ? '…' : total;
  function group(label: string, rows: Event[], overdue = false, open = false) {
    return rows.length > 0 && <details className={`planner-group${overdue ? ' planner-overdue' : ''}`} open={open}>
      <summary>{label}<span>{rows.length}</span><Icon name="chevron" /></summary>
      <PlannerEvents events={rows} today={today} />
    </details>;
  }
  const content = <><p className="sidebar-caption">{today ? `${error ? 'Данные за' : 'Сегодня,'} ${eventDate(today,today)}` : 'Планы на сегодня'}</p>
    {error && <p className="planner-error" role="alert">{error}{asOf && ` Показаны данные на ${asOf.slice(11,16)}.`}</p>}
    {!loading && <p className="planner-count">Незавершённых сегодня: {total}{events.length > 5 && ` · ближайших показано 5 из ${events.length}`}</p>}
    {loading ? <p className="planner-loading" role="status">Загружаю планы…</p> : visible.length
      ? <PlannerEvents events={visible} today={today} ongoingIds={ongoingIds} nearest />
      : total > 0 ? <p className="planner-empty">Ближайших событий со временем нет.</p>
      : !error && <div className="sidebar-empty"><span className="empty-rail" aria-hidden="true"><i /><i /><i /></span>
        <p>День начинается<br />с одной мысли.</p><span>На сегодня незавершённых событий нет.</span></div>}
    {group('Без времени',withoutTime,false,withoutTime.length <= 1)}
    {group('Просрочено сегодня',overdueToday,true,overdueToday.length <= 1)}
    {group('Просрочено раньше',overdueEarlier,true)}
    {undatedCount > 0 && <p className="planner-undated">Без даты: {undatedCount} · не включены в сегодняшний счётчик</p>}
    <button type="button" className="button button-outline sidebar-button" disabled={disabled} onClick={onShowPlans}>
      Показать все планы<Icon name="arrow" /></button>
    </>;
  return <aside className="upcoming-sidebar" aria-label="Ближайшие события">
    <div className="sidebar-desktop"><h2>Ближайшее <span title="Незавершённых событий сегодня" aria-label={`Незавершённых событий сегодня: ${badge}`}>{badge}</span></h2>{content}
      <div className="quiet-note"><Icon name="sparkle" /><div><p>Меньше в голове.<br />Больше в моменте.</p><span>Я помогу держать фокус на важном<br />и напомню о главном — когда нужно.</span></div></div></div>
    <details className="sidebar-mobile"><summary><span><Icon name="clock" />Ближайшее
      <span className="count-badge" title="Незавершённых событий сегодня" aria-label={`Незавершённых событий сегодня: ${badge}`}>{badge}</span></span><Icon name="chevron" /></summary>{content}</details>
  </aside>;
}
