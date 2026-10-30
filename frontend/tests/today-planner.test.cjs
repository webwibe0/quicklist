const test = require('node:test');
const assert = require('node:assert/strict');
const React = require('react');
const {JSDOM} = require('jsdom');
const {load} = require('./load-component.cjs');
const {renderToStaticMarkup} = require('react-dom/server');
const {act} = React;

function item(id, clock='15:00:00') {return {id,type:'task',title:'Account '+id,event_date:'2026-10-28',event_time:clock,end_time:null,status:'pending',source:'user',type_inferred:false};}
function plan(extra={}) {return {today:'2026-10-28',timezone:'Europe/Moscow',as_of:'2026-10-28T12:00:00+03:00',total_today:0,upcoming:[],without_time:[],overdue_today:[],overdue_earlier:[],ongoing_ids:[],undated_count:0,...extra};}

async function mount(t, fetcher) {
  const dom=new JSDOM('<div id="root"></div>',{url:'http://localhost:3003',pretendToBeVisual:true});
  const original=new Map();
  for(const [key,value] of Object.entries({window:dom.window,document:dom.window.document,IS_REACT_ACT_ENVIRONMENT:true,fetch:fetcher})){
    original.set(key,Object.getOwnPropertyDescriptor(globalThis,key));Object.defineProperty(globalThis,key,{value,writable:true,configurable:true});
  }
  let tick;
  dom.window.setInterval=fn=>{tick=fn;return 1;};dom.window.clearInterval=()=>{};
  dom.window.HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','');};
  dom.window.HTMLDialogElement.prototype.close=function(){this.removeAttribute('open');};
  const root=require('react-dom/client').createRoot(document.getElementById('root'));
  const {TodayPlanner}=load('components/today-planner.tsx');
  async function render(accountId='a',revision=0){await act(async()=>root.render(React.createElement(TodayPlanner,{accountId,revision})));}
  t.after(async()=>{await act(async()=>root.unmount());dom.window.close();for(const [key,value] of original)value?Object.defineProperty(globalThis,key,value):delete globalThis[key];});
  await render();
  return {render,tick:async()=>act(async()=>tick()),dom};
}

test('today sidebar counts whole day, caps nearest preview at five, separates untimed and overdue',()=>{
  const {UpcomingSidebar}=load('components/upcoming-sidebar.tsx');
  const doc=new JSDOM(renderToStaticMarkup(React.createElement(UpcomingSidebar,{
    today:'2026-10-28',events:Array.from({length:8},(_,i)=>item('timed-'+i)),total:11,
    withoutTime:[item('untimed',null)],overdueToday:[item('late-1','09:00:00'),item('late-2','10:00:00')],
    overdueEarlier:[{...item('old'),'event_date':'2026-10-27'}],disabled:false,onShowPlans(){},
  }))).window.document;
  const desktop=doc.querySelector('.sidebar-desktop');
  assert.match(desktop.querySelector('.sidebar-caption').textContent,/Сегодня, 28 октября/);
  assert.equal(desktop.querySelector('h2>span').textContent,'11');
  assert.equal(desktop.querySelectorAll('.planner-nearest>li').length,5);
  assert.match(desktop.textContent,/Без времени|Просрочено сегодня|Просрочено раньше/);
  assert.doesNotMatch(desktop.textContent,/Из текущего диалога/);
});

test('all plans opens a real dialog with every today event and no chat or LLM request',async t=>{
  const requests=[];
  await mount(t,async(url,init)=>{requests.push({url,init});return Response.json(plan({total_today:8,upcoming:Array.from({length:7},(_,i)=>item('day-'+i)),without_time:[item('no-time',null)]}));});
  const button=document.querySelector('.sidebar-desktop .sidebar-button');
  await act(async()=>button.click());
  const dialog=document.querySelector('dialog');assert.equal(dialog.open,true);
  assert.equal(dialog.querySelectorAll('.timeline-event').length,8);
  assert.match(dialog.textContent,/Account day-6|Account no-time/);
  assert.ok(requests.every(r=>r.url==='/api/v1/events/today' && (!r.init.method||r.init.method==='GET')));
});

test('revision refresh and periodic/focus refresh update account data; no conversation dependency',async t=>{
  let version=0;const calls=[];
  const ui=await mount(t,async(url,init)=>{calls.push({url,init});return Response.json(plan({total_today:version,upcoming:version?[item('updated')]:[]}));});
  assert.match(document.querySelector('.sidebar-desktop h2').textContent,/0/);
  version=1;await ui.render('a',1);
  assert.match(document.querySelector('.sidebar-desktop').textContent,/Account updated/);
  version=0;await ui.tick();assert.match(document.querySelector('.sidebar-desktop h2').textContent,/0/);
  version=1;await act(async()=>window.dispatchEvent(new window.Event('focus')));
  assert.match(document.querySelector('.sidebar-desktop h2').textContent,/1/);
  assert.equal(calls.length,4);
});

test('late old-owner response is discarded; old plan never renders after account change',async t=>{
  let resolve,oldSignal;
  const ui=await mount(t,(url,init)=>{oldSignal=init.signal;return new Promise(r=>resolve=r);});
  const old=resolve;
  global.fetch=async()=>Response.json(plan({total_today:1,upcoming:[item('owner-b')]}));
  await ui.render('b',0);assert.equal(oldSignal.aborted,true);
  await act(async()=>old(Response.json(plan({total_today:1,upcoming:[item('owner-a-secret')]}))));
  assert.match(document.body.textContent,/Account owner-b/);
  assert.doesNotMatch(document.body.textContent,/owner-a-secret/);
});

test('failed refresh keeps explicitly stale data instead of a false empty day',async t=>{
  const ui=await mount(t,async()=>Response.json(plan({total_today:1,upcoming:[item('saved')]})));
  global.fetch=async()=>{throw new TypeError('network');};await ui.tick();
  assert.match(document.querySelector('.sidebar-desktop').textContent,/Account saved/);
  assert.match(document.querySelector('.sidebar-desktop').textContent,/не обновлены|Не удалось обновить/);
  assert.match(document.querySelector('.sidebar-caption').textContent,/Данные за/);
});
