"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { CalendarProvider, GoogleCalendarControls } from "@/components/google-calendar";
import { apiFetch, type Account } from "@/lib/api";
import { restoreHistory } from "@/lib/chat-state";
import { AppShell } from "@/components/app-shell";
import { Header, type ConnectionStatus } from "@/components/header";
import { ChatThread } from "@/components/chat-thread";
import { Composer } from "@/components/composer";
import { TodayPlanner } from "@/components/today-planner";
import { applyAssistantResponse } from "@/lib/chat-state";
import { dateKey } from "@/lib/events";
import { requestError } from "@/lib/errors";
import { assistantState } from "@/components/assistant-state";
import { useVoice } from "@/components/use-voice";
import { recordingBusy, speechText } from "@/lib/voice";
import type { AssistantResponse, ChatEntry, Event, EventAction } from "@/types/assistant";

const STORAGE = "gorkycode-chat-v1";

export default function QuickListApp({account}: {account: Account}) {
  const pointerKey = `quicklist-conversation:${account.id}`;
  const boundary = useRef<HTMLDivElement>(null);
  const [entries, setEntries] = useState<ChatEntry[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [pendingText, setPendingText] = useState("");
  const [ready, setReady] = useState(false);
  const [logoutBusy, setLogoutBusy] = useState(false);
  const [error, setError] = useState("");
  const [today, setToday] = useState("");
  const [planRevision, setPlanRevision] = useState(0);
  const [status, setStatus] = useState<ConnectionStatus>({text: "Подключение…", tone: "waiting"});
  const end = useRef<HTMLDivElement>(null);
  const field = useRef<HTMLTextAreaElement>(null);
  const inFlight = useRef(false);
  const voice = useVoice((text, submit) => {
    // Never overwrite a draft or silently send it with a voice command.
    if (submit) {
      void execute(text, "/api/v1/assistant/messages", {message: text});
    } else if (voice.autoSend && !input.trim()) {
      setInput(text);
      void execute(text, "/api/v1/assistant/messages", {message: text}, true);
    } else { setInput(previous => previous.trim() ? `${previous}\n${text}` : text); field.current?.focus(); }
  });
  const voiceBusy = recordingBusy(voice.recording);
  const autoSpeak = useRef(false);
  autoSpeak.current = voice.autoSpeak;

  useEffect(() => {
    let cancelled = false;
    const endSession = () => { if (boundary.current) boundary.current.hidden = true; voice.reset(); window.location.replace('/login'); };
    const verify = async () => {
      if (boundary.current) boundary.current.hidden = true;
      try {
        const response = await apiFetch('/api/v1/auth/me');
        if (!response.ok || (await response.json()).id !== account.id) return endSession();
        if (!cancelled && boundary.current) boundary.current.hidden = false;
      } catch { endSession(); }
    };
    const hide = () => { if (boundary.current) boundary.current.hidden = true; };
    window.addEventListener('pageshow', verify);
    window.addEventListener('pagehide', hide);
    window.addEventListener('quicklist-session-ended', endSession);
    let channel: BroadcastChannel | undefined;
    if (typeof BroadcastChannel !== 'undefined') { channel = new BroadcastChannel('quicklist-account'); channel.onmessage = endSession; }
    async function load() {
      try {
        let pointer: string | null | undefined;
        const savedPointer = localStorage.getItem(pointerKey);
        if (savedPointer !== null) pointer = JSON.parse(savedPointer).conversation_id;
        if (account.is_legacy_owner) {
          const legacy = JSON.parse(localStorage.getItem(STORAGE) || 'null');
          if (legacy?.conversationId && Array.isArray(legacy.entries)) {
            const check = await apiFetch(`/api/v1/conversations/${legacy.conversationId}`);
            if (check.ok) {
              const imported = await apiFetch(`/api/v1/conversations/${legacy.conversationId}/legacy-history`, {
                method:'POST',headers:{'Content-Type':'application/json'},
                body:JSON.stringify({entries:legacy.entries.slice(-100).map((e: ChatEntry) => ({role:e.role,text:e.text}))}),
              });
              if (imported.ok) { pointer = pointer ?? legacy.conversationId; localStorage.removeItem(STORAGE); }
            }
          }
        }
        if (pointer === undefined) {
          const response = await apiFetch('/api/v1/conversations');
          if (!response.ok) throw new Error();
          pointer = (await response.json())[0]?.id ?? null;
        }
        if (pointer) {
          const response = await apiFetch(`/api/v1/conversations/${pointer}`);
          if (response.ok) {
            const history = await response.json();
            if (!cancelled) { setConversationId(history.conversation_id); setEntries(restoreHistory(history.entries)); }
          } else if (response.status !== 404) throw new Error();
        }
        if (!cancelled) setReady(true);
      } catch { if (!cancelled) setError('Не удалось загрузить ваш диалог. Обновите страницу.'); }
    }
    void load();
    setToday(dateKey());
    const refreshHealth = () => fetch('/api/health').then(async response => {
      if (!response.ok) throw new Error();
      const health = await response.json();
      if (cancelled) return;
      setStatus(health.test_mode ? {text:'Тестовый режим',tone:'test'} : health.llm_configured && health.database === 'ok' ?
        {text:'Готов к работе',tone:'online'} : {text:'Нужен ключ на сервере',tone:'offline'});
    }).catch(() => {if (!cancelled) setStatus({text:'Сервер недоступен',tone:'offline'});});
    void refreshHealth();
    const interval = setInterval(() => {setToday(dateKey()); void refreshHealth();}, 60000);
    return () => {cancelled=true;clearInterval(interval);channel?.close();window.removeEventListener('pageshow',verify);
      window.removeEventListener('pagehide',hide);window.removeEventListener('quicklist-session-ended',endSession);};
  }, [account.id, account.is_legacy_owner, pointerKey]);

  useEffect(() => {
    if (!ready) return;
    try { localStorage.setItem(pointerKey, JSON.stringify({conversation_id: conversationId})); }
    catch { /* Only a pointer is cached; transcripts remain on the authenticated server. */ }
  }, [conversationId, ready, pointerKey]);

  async function logout() {
    if (logoutBusy) return;
    setLogoutBusy(true);
    voice.reset(); setEntries([]); setReady(false);
    try {
      const response = await apiFetch('/api/v1/auth/logout', {method:'POST'});
      if (!response.ok && response.status !== 401) throw new Error();
      if (typeof BroadcastChannel !== 'undefined') { const channel = new BroadcastChannel('quicklist-account'); channel.postMessage('logout'); channel.close(); }
      window.location.replace('/login');
    } catch { setError('Не удалось выйти. Обновите страницу и повторите.'); }
    finally { setLogoutBusy(false); }
  }

  useEffect(() => {
    if (entries.length || busy || error) end.current?.scrollIntoView({behavior: "instant"});
  }, [entries, busy, error]);

  async function execute(text: string, endpoint: string, payload: Record<string, string>, clearInput = false) {
    if (inFlight.current || !ready) return;
    inFlight.current = true;
    voice.stopPlayback();
    setBusy(true); setPendingText(text); setError("");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 90000);
    try {
      const response = await apiFetch(endpoint, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({conversation_id: conversationId, ...payload}), signal: controller.signal,
      });
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Не удалось обработать сообщение.");
      const result = body as AssistantResponse;
      setConversationId(result.conversation_id);
      setEntries(previous => applyAssistantResponse(previous, text, result));
      if (autoSpeak.current && !document.hidden) voice.play(String(entries.length + 1), speechText({role: "assistant", text: result.message, missing_fields: result.missing_fields}));
      if (clearInput) setInput("");
    } catch (failure) {
      setError(requestError(failure));
    } finally {
      clearTimeout(timeout); inFlight.current = false; setBusy(false); setPendingText("");
      setPlanRevision(value=>value+1);
      setTimeout(() => field.current?.focus(), 0);
    }
  }

  function reply(text: string) { if (!voiceBusy) void execute(text, "/api/v1/assistant/messages", {message: text}); }
  function send(event: FormEvent) {
    event.preventDefault();
    if (input.trim() && !voiceBusy) void execute(input.trim(), "/api/v1/assistant/messages", {message: input.trim()}, true);
  }
  function cardAction(action: EventAction, event: Event) {
    if (voiceBusy) return;
    const text = `${action === "DELETE_EVENT" ? "Удалить" : event.type === "meeting" ? "Отметить состоявшейся" : "Отметить выполненным"}: ${event.title}`;
    void execute(text, `/api/v1/events/${event.id}/actions`, {action});
  }
  function newChat() {
    if (inFlight.current) return;
    voice.reset();
    setEntries([]); setConversationId(null); setError(""); setInput("");
    field.current?.focus({preventScroll:true});
  }
  function example(text: string) { setInput(text); field.current?.focus(); }

  return <div ref={boundary}><CalendarProvider eventRevision={entries.length}><AppShell header={<Header calendarControls={<GoogleCalendarControls />} account={account} onLogout={logout} logoutBusy={logoutBusy} status={status} today={today} disabled={busy || !ready} onNewChat={newChat} />}
    sidebar={<TodayPlanner key={account.id} accountId={account.id} revision={planRevision} />}>
    <ChatThread entries={entries} pendingText={pendingText} busy={busy} ready={ready && !voiceBusy} error={error} endRef={end}
      onExample={example} onReply={reply} onAction={cardAction} voice={voice} />
    <Composer value={input} onChange={setInput} onSubmit={send} inputRef={field} busy={busy} ready={ready}
      voice={voice} state={status.tone === "offline" && !busy && !error ? {kind: "error", label: status.text}
        : assistantState(entries, busy, error, ready && status.tone !== "waiting")} />
  </AppShell></CalendarProvider></div>;
}

