"use client";
import {createContext, useContext, useEffect, useState, type ReactNode} from "react";
import {apiFetch} from "@/lib/api";
import type {Event} from "@/types/assistant";

type Connection = {configured: boolean; connected: boolean; sync_enabled: boolean; account_email?: string;
  error_code?: string; pending_count?: number; error_count?: number};
type ItemState = {eligible: boolean; status: string; reason?: string; error_code?: string;
  default_duration_minutes?: number; recurrence_note?: string};
const Context = createContext<{connection: Connection | null; refresh: () => Promise<void>; revision: number; unavailable: boolean} | null>(null);
const base = "/api/v1/calendar/google";
const labels: Record<string,string> = {DATE_REQUIRED:"Нужна дата события", END_TIME_INVALID:"Проверьте окончание",
  EVENT_INACTIVE:"Отменённое событие удаляется из связанного календаря",
  SYNC_DISABLED:"Синхронизация выключена", NOT_CONNECTED:"Подключите календарь", SYNC_BUSY:"Идёт другая синхронизация. Повторите чуть позже",
  REAUTH_REQUIRED:"Google отозвал доступ. Подключите аккаунт заново", GOOGLE_UNAVAILABLE:"Google недоступен. Локальное событие сохранено",
  REMOTE_CHANGED_RETRY:"Событие изменилось в Google. Повторите синхронизацию", REMOTE_ID_CONFLICT:"Связь с Google не подтверждена",
  REMOTE_EVENT_DELETED:"Событие удалено в Google. Локальная запись сохранена. Восстановите событие в Google перед повторной синхронизацией",
  OAUTH_STATE_INVALID:"Подключение не подтверждено. Начните заново", OAUTH_CANCELLED:"Подключение отменено"};
function message(code?: string) {return code ? labels[code] || "Google Calendar: синхронизация не выполнена. Можно повторить." : "";}
async function checked(response: Response) {
  const body = await response.json();
  if (!response.ok) throw new Error(message(body.detail?.code) || "Google Calendar временно недоступен.");
  return body;
}

export function calendarBadge(connection: Connection | null, unavailable = false) {
  if (unavailable) return {label:'недоступен',tone:'error',description:'Не удалось проверить подключение Google Calendar'};
  if (!connection) return {label:'проверяю…',tone:'waiting',description:'Проверяется подключение Google Calendar'};
  if (connection.error_code) return {label:connection.error_code === 'REAUTH_REQUIRED' ? 'подключите заново' : 'ошибка подключения',tone:'error',description:message(connection.error_code)};
  if (!connection.configured) return {label:'не настроен',tone:'waiting',description:'Требуется настройка OAuth на сервере'};
  if (!connection.connected) return {label:'не подключён',tone:'waiting',description:'Подключите Google Calendar'};
  return {label:'подключён',tone:'connected',description:connection.sync_enabled ? 'Google подключён. Синхронизация включена.' : 'Google подключён. Синхронизация выключена.'};
}

function CalendarMark() {
  return <svg className="google-calendar-mark" viewBox="0 0 36 40" aria-hidden="true">
    <path d="M4 2h28a2 2 0 0 1 2 2v28l-6 6H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2Z" fill="#fff"/>
    <path d="M2 10V4a2 2 0 0 1 2-2h28a2 2 0 0 1 2 2v6Z" fill="#4285f4"/>
    <path d="M2 10h6v22H2Z" fill="#4285f4"/><path d="M8 32h20v6H8Z" fill="#34a853"/>
    <path d="M28 10h6v22h-6Z" fill="#fbbc04"/><path d="m28 38 6-6h-6Z" fill="#ea4335"/>
    <path d="M2 32h6v6H4a2 2 0 0 1-2-2Z" fill="#188038"/>
    <text x="18" y="27" textAnchor="middle" fill="#1a73e8" fontFamily="Arial,sans-serif" fontWeight="700" fontSize="17">31</text>
  </svg>;
}

export function CalendarProvider({children,eventRevision = 0}: {children: ReactNode; eventRevision?: number}) {
  const [connection,setConnection] = useState<Connection | null>(null);
  const [revision,setRevision] = useState(0);
  const [unavailable,setUnavailable] = useState(false);
  async function refresh() {try {setConnection(await checked(await apiFetch(base+"/status")));setUnavailable(false);setRevision(value=>value+1);} catch (failure) {setUnavailable(true);throw failure;}}
  useEffect(()=>{
    let active=true, polls=0;let timer: ReturnType<typeof setTimeout> | undefined;
    const load=async()=>{try{const value=await checked(await apiFetch(base+"/status"));if(active){setConnection(value);setUnavailable(false);if(value.pending_count && polls++<15)timer=setTimeout(()=>void load(),2000);}}catch{if(active)setUnavailable(true);}};
    void load();return()=>{active=false;clearTimeout(timer);};
  },[eventRevision]);
  return <Context.Provider value={{connection,refresh,revision,unavailable}}>{children}</Context.Provider>;
}

export function GoogleCalendarControls() {
  const context = useContext(Context);
  const [busy,setBusy] = useState(false), [error,setError] = useState("");
  useEffect(()=>{
    const result = new URLSearchParams(window.location.search).get('calendar');
    if(result && result!=='connected')setError(message(result));
    if(result)window.history.replaceState(null,'',window.location.pathname+window.location.hash);
  },[]);
  if(!context) return null;
  const {connection,refresh}=context;
  const badge = calendarBadge(connection,context.unavailable);
  async function action(path: string, body?: object) {
    setBusy(true);setError("");
    try {
      const response=await checked(await apiFetch(base+path,{method:'POST',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined}));
      if(response.authorization_url)window.location.assign(response.authorization_url);
      else await refresh();
    } catch(failure) {setError(failure instanceof Error?failure.message:'Google Calendar недоступен');}
    finally {setBusy(false);}
  }
  return <details className="calendar-controls"><summary className={`calendar-summary calendar-${badge.tone}`} title={badge.description}>
    <CalendarMark /><span className="calendar-summary-copy"><span>Google Calendar</span><small><i />{badge.label}</small></span>
  </summary>
    <div className="calendar-popover">
      <strong>Google Calendar</strong>
      {context.unavailable && <p role="alert">Не удалось проверить подключение. Откройте страницу заново.</p>}
      {!connection?.configured ? <p>Требуется настройка OAuth на сервере.</p> : <>
        <p>{connection.connected?connection.account_email:'Календарь не подключён'}</p>
        {connection.connected && <p className="calendar-sync-note">{connection.sync_enabled ? 'Синхронизация включена' : 'Синхронизация выключена'}</p>}
        {!connection.connected || connection.error_code==='REAUTH_REQUIRED'
          ? <button type="button" className="button button-outline" disabled={busy} onClick={()=>void action('/connect')}>Подключить Google Calendar</button>
          : <><label><input type="checkbox" checked={connection.sync_enabled} disabled={busy}
            onChange={event=>void action('/sync',{enabled:event.target.checked})}/> Синхронизация</label>
            <p>Автоматически: новые события и изменения уже связанных. Старые записи сами не экспортируются.</p>
            </>}
        {connection.connected && <button type="button" className="button button-quiet" disabled={busy} onClick={()=>void action('/disconnect')}>Отключить</button>}
        {(Boolean(connection.pending_count) || Boolean(connection.error_count)) && <p role="status">Ожидают: {connection.pending_count||0}. Ошибок: {connection.error_count||0}.
          <button type="button" className="button button-quiet" disabled={busy || !connection.sync_enabled} onClick={()=>void action('/retry')}>Повторить синхронизацию</button></p>}
      </>}
      <p>Без окончания: встречи — 60 мин, напоминания и задачи — 15 мин. Без времени — весь день. Без даты — не экспортируется.</p>
      <p>Повторения: только выбранная дата, без экспорта серии. При отключении ранее созданные события остаются в Google.</p>
      {(error || connection?.error_code) && <p role="alert">{error || message(connection?.error_code)}</p>}
    </div>
  </details>;
}

export function GoogleEventSync({event,disabled}: {event: Event; disabled: boolean}) {
  const context=useContext(Context);
  const [state,setState]=useState<ItemState | null>(null),[busy,setBusy]=useState(false),[error,setError]=useState("");
  const connected=Boolean(context?.connection?.connected);
  useEffect(()=>{
    if(!connected)return;
    let active=true;let timer: ReturnType<typeof setTimeout> | undefined;let polls=0;
    const load=async()=>{try{const value=await checked(await apiFetch(`${base}/events/${event.id}`));if(active){setState(value);if(value.status==='pending' && polls++<15)timer=setTimeout(()=>void load(),2000);}}catch{if(active)setError('Статус Google Calendar недоступен');}};
    void load();return()=>{active=false;clearTimeout(timer);};
  },[connected,context?.revision,event.id,event.title,event.event_date,event.event_time,event.end_time,event.status]);
  if(!connected || !context)return null;
  async function sync(){setBusy(true);setError('');try{setState(await checked(await apiFetch(`${base}/events/${event.id}/sync`,{method:'POST'})));await context!.refresh();}
    catch(failure){setError(failure instanceof Error?failure.message:'Синхронизация не выполнена');}finally{setBusy(false);}}
  return <div className="calendar-event-sync"><span role="status">Google: {state?.status==='synced'?'синхронизировано':state?.status==='pending'?'ожидает':state?.status==='error'?'ошибка':'не связано'}</span>
    {state?.eligible && <button type="button" className="button button-small button-quiet" disabled={disabled||busy||!context.connection?.sync_enabled} onClick={()=>void sync()}>{busy?'Синхронизация…':state.status==='error'?'Повторить':'В Google Calendar'}</button>}
    {state?.default_duration_minutes && <small>Без окончания: {state.default_duration_minutes} мин</small>}
    {state?.recurrence_note && <small>{state.recurrence_note}</small>}
    {(error || state?.error_code || state?.reason) && <small role={error||state?.error_code?'alert':undefined}>{error || message(state?.error_code || state?.reason)}</small>}
  </div>;
}
