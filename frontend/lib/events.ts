import type { ChatEntry, Event } from "@/types/assistant";

export function dateKey(now = new Date()) {
  return new Intl.DateTimeFormat("sv-SE", {timeZone: "Europe/Moscow"}).format(now);
}

export function eventDate(value: string, today = dateKey()) {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "numeric", month: "long", timeZone: "UTC",
    ...(value.slice(0, 4) !== today.slice(0, 4) ? {year: "numeric" as const} : {}),
  }).format(new Date(`${value}T12:00:00Z`));
}

export function dayLabel(value: string | null, today: string) {
  if (!value) return "Без даты";
  if (value === today) return "Сегодня";
  const tomorrow = new Date(`${today}T12:00:00Z`);
  tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
  if (value === tomorrow.toISOString().slice(0, 10)) return "Завтра";
  return eventDate(value, today);
}

export function eventTime(event: Event) {
  return event.event_time
    ? event.event_time.slice(0, 5) + (event.end_time ? `–${event.end_time.slice(0, 5)}` : "")
    : "";
}

export function upcomingEvents(entries: ChatEntry[], today: string): Event[] {
  const latest = new Map<string, Event>();
  entries.forEach(entry => entry.events?.forEach(event => latest.set(event.id, event)));
  return [...latest.values()].filter(event => event.status === "pending" && (!event.event_date || event.event_date >= today))
    .sort((a, b) => `${a.event_date || "9999"} ${a.event_time || "99"}`
      .localeCompare(`${b.event_date || "9999"} ${b.event_time || "99"}`)).slice(0, 6);
}
