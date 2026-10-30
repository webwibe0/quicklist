"use client";
import {useEffect, useRef, useState} from 'react';
import {apiFetch} from '@/lib/api';
import {eventDate} from '@/lib/events';
import type {TodayPlan} from '@/types/assistant';
import {PlannerEvents, UpcomingSidebar} from './upcoming-sidebar';

export function useTodayPlan(accountId: string, revision: number) {
  const [state,setState] = useState<{owner: string; data: TodayPlan | null; loading: boolean; error: string}>(
    {owner:accountId,data:null,loading:true,error:''});
  useEffect(()=>{
    let active=true, generation=0;
    let controller: AbortController | undefined;
    setState(previous=>previous.owner === accountId ? {...previous,loading:!previous.data}
      : {owner:accountId,data:null,loading:true,error:''});
    async function load() {
      const ticket=++generation;
      controller?.abort();controller=new AbortController();
      const request=controller;
      const timeout=window.setTimeout(()=>request.abort(),8000);
      try {
        const response=await apiFetch('/api/v1/events/today',{signal:request.signal});
        if (!response.ok) throw new Error('Daily plan unavailable');
        const data=await response.json() as TodayPlan;
        if(active && ticket===generation) setState({owner:accountId,data,loading:false,error:''});
      } catch {
        if(active && ticket===generation) setState(previous=>({owner:accountId,
          data:previous.owner===accountId ? previous.data : null,loading:false,error:'Не удалось обновить планы.'}));
      } finally {window.clearTimeout(timeout);}
    }
    const visible=()=>{if(!document.hidden) void load();};
    void load();
    const timer=window.setInterval(visible,60000);
    window.addEventListener('focus',visible);document.addEventListener('visibilitychange',visible);
    return()=>{active=false;controller?.abort();window.clearInterval(timer);window.removeEventListener('focus',visible);document.removeEventListener('visibilitychange',visible);};
  },[accountId,revision]);
  return state.owner===accountId ? state : {owner:accountId,data:null,loading:true,error:''};
}

export function TodayPlanner({accountId,revision}: {accountId: string; revision: number}) {
  const {data,loading,error}=useTodayPlan(accountId,revision);
  const [open,setOpen]=useState(false);
  const dialog=useRef<HTMLDialogElement>(null);
  useEffect(()=>{setOpen(false);},[accountId]);
  useEffect(()=>{
    if(open && dialog.current && !dialog.current.open) dialog.current.showModal();
    if(!open && dialog.current?.open) dialog.current.close();
  },[open]);
  return <>
    <UpcomingSidebar events={data?.upcoming || []} today={data?.today || ''} total={data?.total_today || 0}
      withoutTime={data?.without_time} overdueToday={data?.overdue_today} overdueEarlier={data?.overdue_earlier}
      ongoingIds={data?.ongoing_ids} undatedCount={data?.undated_count} loading={loading} error={error} asOf={data?.as_of}
      disabled={!data} onShowPlans={()=>setOpen(true)} />
    <dialog className="today-plans-dialog" ref={dialog} aria-labelledby="today-plans-title"
      onClose={()=>setOpen(false)} onCancel={()=>setOpen(false)}>
      <div className="today-plans-heading"><div><p className="eyebrow">Ваш день</p><h2 id="today-plans-title">Все планы на сегодня</h2></div>
        <button type="button" className="button button-outline" onClick={()=>setOpen(false)} aria-label="Закрыть планы на сегодня">Закрыть</button></div>
      {data && <><p className="today-plans-caption">{eventDate(data.today,data.today)} · Незавершённых: {data.total_today}</p>
        {error && <p role="alert" className="planner-error">{error}</p>}
        {data.upcoming.length>0 && <section><h3>По времени</h3><PlannerEvents events={data.upcoming} today={data.today} ongoingIds={data.ongoing_ids}/></section>}
        {data.without_time.length>0 && <section><h3>Без времени</h3><PlannerEvents events={data.without_time} today={data.today}/></section>}
        {data.overdue_today.length>0 && <section className="planner-overdue"><h3>Просрочено сегодня</h3><PlannerEvents events={data.overdue_today} today={data.today}/></section>}
        {data.total_today===0 && <p>На сегодня незавершённых событий нет.</p>}
      </>}
    </dialog>
  </>;
}
