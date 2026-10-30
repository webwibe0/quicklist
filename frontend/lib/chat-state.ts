import type { AssistantResponse, ChatEntry, Event } from "@/types/assistant";

export function applyAssistantResponse(entries: ChatEntry[], text: string, result: AssistantResponse): ChatEntry[] {
  const changes = new Map(result.events.map(event => [event.id, event]));
  const refresh = (events: Event[]) => events.map(event => changes.get(event.id) || event)
    .filter(event => event.status !== "deleted");
  return [...entries.map(entry => entry.events ? {...entry, events: refresh(entry.events)} : entry),
    {role: "user", text},
    {role: "assistant", text: result.message, events: refresh(result.events),
      action: result.action, requires_input: result.requires_input, missing_fields: result.missing_fields}];
}


export type HistoryEntry = {role: "user" | "assistant"; text: string; response?: AssistantResponse};
export function restoreHistory(rows: HistoryEntry[]): ChatEntry[] {
  let entries: ChatEntry[] = [];
  let pending: string | undefined;
  for (const row of rows) {
    if (row.role === "user") {
      if (pending !== undefined) entries.push({role:"user",text:pending});
      pending = row.text;
    } else if (row.response && pending !== undefined) {
      entries = applyAssistantResponse(entries,pending,row.response); pending=undefined;
    } else {
      if (pending !== undefined) {entries.push({role:"user",text:pending});pending=undefined;}
      entries.push({role:"assistant",text:row.text});
    }
  }
  if (pending !== undefined) entries.push({role:"user",text:pending});
  return entries;
}
