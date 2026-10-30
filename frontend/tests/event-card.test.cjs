const assert = require("node:assert/strict");



const test = require("node:test");
const React = require("react");
const {renderToStaticMarkup} = require("react-dom/server");


const {load} = require('./load-component.cjs');
const {EventCard} = load('components/event-card.tsx');
const event = {id: "test", type: "meeting", title: "Зум с коллегами", event_date: "2026-10-16",
  event_time: "12:00:00", end_time: "23:00:00", type_inferred: false, source: "user", status: "pending"};

test("meeting card renders both interval boundaries", () => {
  const html = renderToStaticMarkup(React.createElement(EventCard, {event}));
  assert.ok(html.includes('dateTime="2026-10-16"') && html.includes("12:00–23:00"), html);
});

test("single-time card keeps its previous format", () => {
  const html = renderToStaticMarkup(React.createElement(EventCard, {event: {...event, end_time: null}}));
  assert.ok(html.includes('dateTime="2026-10-16"') && html.includes("12:00"), html);
  assert.ok(!html.includes("12:00–"), html);
});

test("unscheduled task keeps its previous format", () => {
  const html = renderToStaticMarkup(React.createElement(EventCard, {event: {...event, type: "task",
    event_date: null, event_time: null, end_time: null}}));
  assert.ok(html.includes("Без даты"), html);
  assert.ok(!html.includes("12:00"), html);
});

function buttons(element) {
  if (!element || typeof element !== "object") return [];
  if (element.type === "button") return [element];
  return React.Children.toArray(element.props?.children).flatMap(buttons);
}

test("active task buttons dispatch complete/delete with the selected event", () => {
  const calls = [];
  const task = {...event, type: "task"};
  const rendered = EventCard({event: task, onAction: (action, selected) => calls.push([action, selected.id])});
  const actions = buttons(rendered);
  assert.deepEqual(actions.map(button => button.props.children.filter(child => typeof child === "string").join("")), ["Выполнено", "Удалить"]);
  actions.forEach(button => button.props.onClick());
  assert.deepEqual(calls, [["COMPLETE_EVENT", task.id], ["DELETE_EVENT", task.id]]);
  assert.equal(task.status, "pending"); // Clicking never optimistically changes status.
});

test("meeting can be marked held, completed task has marker and no complete button", () => {
  const onAction = () => {};
  assert.deepEqual(buttons(EventCard({event, onAction})).map(button => button.props.children.filter(child => typeof child === "string").join("")), ["Состоялась", "Удалить"]);
  const completed = {...event, type: "task", status: "completed"};
  const html = renderToStaticMarkup(React.createElement(EventCard, {event: completed, onAction}));
  assert.ok(html.includes("event-completed") && html.includes('role="status">Выполнено'));
  assert.deepEqual(buttons(EventCard({event: completed, onAction})).map(button => button.props.children.filter(child => typeof child === "string").join("")), ["Удалить"]);
});

test("dataset cards can complete/delete, disabled cards disable both buttons, deleted cards disappear", () => {
  const onAction = () => {};
  assert.equal(buttons(EventCard({event: {...event, source: "dataset"}, onAction})).length, 2);
  assert.ok(buttons(EventCard({event: {...event, type: "task"}, onAction, disabled: true}))
    .every(button => button.props.disabled));
  assert.equal(renderToStaticMarkup(React.createElement(EventCard, {event: {...event, status: "deleted"}, onAction})), "");
});

test("historical imported meeting dispatches held and delete for its exact ID", () => {
  const selected = {...event, source: "dataset", event_date: "2024-01-01"};
  const calls = [];
  const actions = buttons(EventCard({event: selected, onAction: (action, item) => calls.push([action, item.id])}));
  assert.equal(actions.length, 2);
  actions.forEach(button => button.props.onClick());
  assert.deepEqual(calls, [["COMPLETE_EVENT", selected.id], ["DELETE_EVENT", selected.id]]);
});

test("meeting without time has explicit label and retains its date and meeting type", () => {
  const html = renderToStaticMarkup(React.createElement(EventCard, {event: {...event, event_time: null, end_time: null}}));
  assert.ok(html.includes("Время не указано") && html.includes("Встреча") && html.includes('dateTime="2026-10-16"'));
  assert.ok(!html.includes("00:00"));
});

test("held imported meetings have the right status and only delete; cancelled events cannot complete", () => {
  const onAction = () => {};
  const held = {...event, source: "dataset", status: "completed"};
  const html = renderToStaticMarkup(React.createElement(EventCard, {event: held, onAction}));
  assert.ok(html.includes('role="status">Состоялась'));
  for (const status of ["completed", "cancelled"]) {
    assert.equal(buttons(EventCard({event: {...held, status}, onAction})).length, 1);
  }
});

const {applyAssistantResponse} = load('lib/chat-state.ts');

test("delete confirmation keeps cards; confirmed delete removes every snapshot without reload", () => {
  const initial = [{role: "assistant", text: "Создано", events: [event]},
    {role: "assistant", text: "Список", events: [event]}];
  const pending = applyAssistantResponse(initial, "Удалить", {
    message: "Удалить?", action: "DELETE_EVENT", events: [], requires_input: true,
  });
  assert.equal(pending.flatMap(entry => entry.events || []).length, 2);
  const removed = applyAssistantResponse(pending, "да", {
    message: "Удалено", action: "DELETE_EVENT", events: [{...event, status: "deleted"}],
  });
  assert.equal(removed.flatMap(entry => entry.events || []).length, 0);
  assert.equal(initial[0].events[0].status, "pending"); // Pure reconciliation, no stale-state mutation.
});

test("completion refreshes all existing card snapshots and preserves end_time", () => {
  const entries = [{role: "assistant", text: "Создано", events: [event]},
    {role: "assistant", text: "Список", events: [event]}];
  const next = applyAssistantResponse(entries, "Выполнено", {
    message: "Выполнено", action: "COMPLETE_EVENT", events: [{...event, status: "completed"}],
  });
  assert.ok(next.flatMap(entry => entry.events || [])
    .every(item => item.status === "completed" && item.end_time === "23:00:00"));
});


const {ConfirmationCard} = load("components/confirmation-card.tsx");
const {LoadingIndicator} = load("components/loading-indicator.tsx");
const {Composer, submitOnEnter} = load("components/composer.tsx");
const {ChatThread} = load("components/chat-thread.tsx");
const {EmptyState} = load("components/empty-state.tsx");
const {UpcomingSidebar} = load("components/upcoming-sidebar.tsx");
const {upcomingEvents, dayLabel, dateKey} = load("lib/events.ts");
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props));

test("confirmation dispatches ordinary yes/no and disables old or busy actions", () => {
  const replies = [];
  const props = {text: "Удалить?", action: "DELETE_EVENT", active: true, disabled: false, onReply: text => replies.push(text)};
  const actions = buttons(ConfirmationCard(props));
  assert.deepEqual(actions.map(button => button.props.children), ["Удалить", "Отмена"]);
  actions.forEach(button => button.props.onClick());
  assert.deepEqual(replies, ["да", "нет"]);
  for (const override of [{active: false}, {disabled: true}]) {
    assert.ok(buttons(ConfirmationCard({...props, ...override})).every(button => button.props.disabled));
  }
});

test("assistant reconciliation preserves backend clarification metadata", () => {
  const result = {message: "Создать два?", events: [], action: "CREATE_EVENT", requires_input: true, missing_fields: ["confirmation"]};
  const latest = applyAssistantResponse([], "создай", result).at(-1);
  assert.equal(latest.action, result.action);
  assert.equal(latest.requires_input, true);
  assert.deepEqual(latest.missing_fields, ["confirmation"]);
});

const threadProps = {entries: [], pendingText: "Запрос", busy: false, ready: true, error: "",
  endRef: {current: null}, onExample() {}, onReply() {}, onAction() {}};

test("only latest assistant confirmation can be answered with buttons", () => {
  const pending = {role: "assistant", text: "Создать?", action: "CREATE_EVENT", requires_input: true, missing_fields: ["confirmation"]};
  const html = render(ChatThread, {...threadProps, entries: [pending, {role: "user", text: "нет"}, pending]});
  assert.equal((html.match(/disabled=""/g) || []).length, 2);
  assert.equal((html.match(/Подтверждение действия/g) || []).length, 2);
  const resolved = render(ChatThread, {...threadProps, entries: [pending, {role: "assistant", text: "Готово"}]});
  assert.equal((resolved.match(/disabled=""/g) || []).length, 2);
});

test("thinking is accessible, request visible immediately, errors compact and clarification distinct", () => {
  const loading = render(LoadingIndicator, {});
  assert.ok(loading.includes('role="status"') && loading.includes('aria-live="polite"') && loading.includes("Разбираю запрос"));
  const pending = render(ChatThread, {...threadProps, busy: true});
  assert.ok(pending.includes("Запрос") && !pending.includes("Что запланируем?"));
  const error = render(ChatThread, {...threadProps, error: "Сервер недоступен"});
  assert.ok(error.includes('role="alert"') && error.includes("Сервер недоступен"));
  const clarification = render(ChatThread, {...threadProps, entries: [{role: "assistant", text: "Во сколько?", requires_input: true, missing_fields: ["time"]}]});
  assert.ok(clarification.includes("Нужно уточнить") && !clarification.includes("Подтверждение действия"));
});

test("composer disables repeated/empty sends and microphone needs its controller", () => {
  const props = {value: "", onChange() {}, onSubmit() {}, inputRef: {current: null}, busy: false, ready: true};
  for (const override of [{}, {value: "текст", busy: true}, {value: "текст", ready: false}]) {
    const html = render(Composer, {...props, ...override});
    assert.match(html, /disabled="" aria-label="Отправить сообщение"/);
    assert.match(html, /disabled="" aria-label="Начать голосовой ввод"/);
  }
  const html = render(Composer, {...props, value: "текст"});
  assert.ok(html.includes('id="message"') && html.includes('maxLength="4000"'));
  assert.ok(!html.includes('disabled="" aria-label="Отправить сообщение"'));
});

test("Enter submits while Shift+Enter and IME composition retain natural textarea behavior", () => {
  let submitted = 0, prevented = 0;
  const event = {key: "Enter", shiftKey: false, nativeEvent: {isComposing: false},
    preventDefault() {prevented++;}, currentTarget: {form: {requestSubmit() {submitted++;}}}};
  submitOnEnter(event);
  submitOnEnter({...event, shiftKey: true});
  submitOnEnter({...event, nativeEvent: {isComposing: true}});
  assert.equal(submitted, 1); assert.equal(prevented, 1);
});

test("welcome examples only prefill via callback, including a natural clarification example", () => {
  const values = [];
  const examples = buttons(EmptyState({disabled: false, onExample: value => values.push(value)}));
  assert.equal(examples.length, 4);
  examples.forEach(button => button.props.onClick());
  assert.ok(values.includes("Что у меня завтра?") && values.some(value => value.includes("позвонить Ивану")));
});

test("upcoming derives latest real snapshots, excludes past/deleted/completed and limits six", () => {
  const dated = {...event, id: "dated", event_date: "2026-10-16"};
  const entries = [{events: [dated, {...event, id: "completed"}, {...event, id: "deleted"}, {...event, id: "past", event_date: "2026-10-14"},
    {...event, id: "unscheduled", event_date: null}, ...Array.from({length: 7}, (_, i) => ({...event, id: `next${i}`, event_date: "2026-10-17"}))]},
    {events: [{...dated, title: "Новое название"}, {...event, id: "completed", status: "completed"}, {...event, id: "deleted", status: "deleted"}]}];
  const upcoming = upcomingEvents(entries, "2026-10-15");
  assert.equal(upcoming.length, 6);
  assert.equal(upcoming[0].title, "Новое название");
  assert.ok(upcoming.every(event => !["past", "completed", "deleted"].includes(event.id)));
  assert.equal(upcomingEvents([{events: [{...event, event_date: null}]}], "2026-10-15")[0].id, event.id);
});

test("Moscow day boundary and tomorrow across year boundary are correct", () => {
  assert.equal(dateKey(new Date("2026-12-31T22:00:00Z")), "2027-01-01");
  assert.equal(dayLabel("2027-01-01", "2026-12-31"), "Завтра");
});

test("sidebar has responsive disclosure, no fabricated events, and explicit plans action", () => {
  const html = render(UpcomingSidebar, {events: [], today: "2026-10-15", disabled: false, onShowPlans() {}});
  assert.ok(html.includes("sidebar-desktop") && html.includes('<details class="sidebar-mobile"'));
  assert.ok(html.includes("Сегодня, 15 октября") && !html.includes(event.title));
  const populated = render(UpcomingSidebar, {events: [event], today: "2026-10-15", disabled: false, onShowPlans() {}});
  assert.ok(populated.includes("Завтра") && populated.includes("12:00–23:00"));
});

const {requestError} = load("lib/errors.ts");
test("network/invalid JSON errors are human-readable Russian without automatic retries", () => {
  for (const failure of [new TypeError("Failed to fetch"), new SyntaxError("Unexpected token"), null]) {
    const text = requestError(failure);
    assert.ok(text.includes("Проверьте подключение и планы") && text.includes("могло сохраниться"));
    assert.ok(!text.includes("Failed to fetch") && !text.includes("Unexpected token"));
  }
  assert.equal(requestError(new Error("Нужно уточнить запрос.")), "Нужно уточнить запрос.");
  const aborted = new Error(); aborted.name = "AbortError";
  assert.ok(requestError(aborted).includes("не получен вовремя"));
});


const {assistantState, AssistantState} = load("components/assistant-state.tsx");
const {VoiceButton} = load("components/voice-button.tsx");

test("v2 assistant state follows real loading/error/clarification/confirmation metadata", () => {
  const pending = {role: "assistant", requires_input: true, missing_fields: ["confirmation"]};
  assert.equal(assistantState([], false, "", false).kind, "connecting");
  assert.equal(assistantState([], false, "", true).kind, "idle");
  assert.equal(assistantState([pending], false, "", true).label, "Ожидаю подтверждение");
  assert.equal(assistantState([{...pending, missing_fields: ["time"]}], false, "", true).label, "Уточняю детали");
  assert.equal(assistantState([pending], true, "", true).kind, "loading");
  assert.equal(assistantState([pending], false, "Сбой", true).kind, "error");
  assert.equal(assistantState([pending, {role: "user", text: "да"}, {role: "assistant", requires_input: false}], false, "", true).kind, "idle");
  const html = render(AssistantState, {state: assistantState([pending], false, "", true)});
  assert.ok(html.includes('role="status"') && html.includes('aria-live="polite"'));
});

test("voice control stays disabled until a recorder is attached", () => {
  const voice = VoiceButton({});
  const mic = buttons(voice)[0];
  assert.equal(mic.props.disabled, true);
  assert.equal(mic.props.onClick, undefined);
  assert.equal(mic.props['aria-label'], "Начать голосовой ввод");
  assert.ok(!render(VoiceButton, {}).includes("Скоро"));
});

test("v2 event emphasizes time before title, includes end_time and preserves completed actions", () => {
  const html = render(EventCard, {event});
  assert.ok(html.indexOf('class="event-time"') < html.indexOf('class="event-title"'));
  assert.ok(html.includes("12:00–23:00") && html.includes("type-meeting"));
  const reminder = render(EventCard, {event: {...event, type: "reminder"}});
  assert.ok(reminder.includes("type-reminder"));
});

test("v2 timeline renders actual time and title together and no assumed time for undated tasks", () => {
  const html = render(UpcomingSidebar, {events: [event, {...event, id: "task", type: "task", event_time: null, end_time: null, event_date: null}], today: "2026-10-15", disabled: false, onShowPlans() {}});
  assert.ok(html.includes('class="timeline-list') && html.includes('aria-label="12:00–23:00"'));
  assert.ok(html.includes("Без времени") && html.includes("Без даты"));
  assert.ok(!html.includes("09:00"));
});

test("v2 welcome announces natural language, examples stay real buttons and reduced motion is retained", () => {
  const html = render(EmptyState, {disabled: false, onExample() {}});
  assert.ok(html.includes("Скажите как есть.") && html.includes("Я разберусь."));
  const {readFileSync} = require("node:fs");
  const path = require("node:path");
  const css = readFileSync(path.resolve(__dirname, '../app/globals.css'), 'utf8');
  assert.ok(css.includes('prefers-reduced-motion: reduce') && css.includes('animation: none !important'));
});

