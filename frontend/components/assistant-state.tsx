import type { ChatEntry } from "@/types/assistant";
import { Icon } from "./icons";

export function assistantState(entries: ChatEntry[], busy: boolean, error: string, ready: boolean) {
  if (busy) return {kind: "loading", label: "Разбираю запрос…"};
  if (error) return {kind: "error", label: "Ответ не получен"};
  if (!ready) return {kind: "connecting", label: "Подключаюсь…"};
  const latest = entries.findLast(entry => entry.role === "assistant");
  if (latest?.requires_input) return latest.missing_fields?.includes("confirmation")
    ? {kind: "confirmation", label: "Ожидаю подтверждение"}
    : {kind: "clarification", label: "Уточняю детали"};
  return {kind: "idle", label: "Готов к команде"};
}

export type AssistantStateValue = ReturnType<typeof assistantState>;

export function AssistantState({state}: {state: AssistantStateValue}) {
  return <div className={`assistant-state state-${state.kind}`} role="status" aria-live="polite">
    <span className="state-orbit"><Icon name={state.kind === "error" ? "alert" : "sparkle"} /></span>
    <span>{state.label}</span><span className="state-caption">QuickList AI</span>
  </div>;
}
