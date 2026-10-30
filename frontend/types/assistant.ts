export type Event = {
  id: string;
  type: "task" | "reminder" | "meeting";
  title: string;
  event_date: string | null;
  event_time: string | null;
  end_time: string | null;
  status: "pending" | "completed" | "cancelled" | "deleted";
  source: "user" | "dataset";
  type_inferred: boolean;
  recurrence_rule?: string | null;
  recurrence_series_id?: string | null;
  recurrence_until?: string | null;
};

export type TodayPlan = {
  today: string; timezone: string; as_of: string; total_today: number;
  upcoming: Event[]; without_time: Event[]; overdue_today: Event[]; overdue_earlier: Event[];
  ongoing_ids: string[]; undated_count: number;
};

export type EventAction = "DELETE_EVENT" | "COMPLETE_EVENT";

export type AssistantResponse = {
  conversation_id: string;
  message: string;
  action: "CREATE_EVENT" | "UPDATE_EVENT" | EventAction | "LIST_EVENTS" | "SEARCH_EVENTS" | "UNKNOWN";
  events: Event[];
  requires_input: boolean;
  missing_fields: string[];
};

export type ChatEntry = {
  role: "user" | "assistant";
  text: string;
  events?: Event[];
  action?: AssistantResponse["action"];
  requires_input?: boolean;
  missing_fields?: string[];
};
