const test = require('node:test');
const assert = require('node:assert/strict');
const React = require('react');
const {JSDOM} = require('jsdom');
const {load} = require('./load-component.cjs');

test('calendar toggle and explicit export use existing session/CSRF and display technical duration', async t=>{
  const dom=new JSDOM('<div id="root"></div>',{url:'http://127.0.0.1:3001'});
  const saved=new Map();
  for(const [key,value] of Object.entries({window:dom.window,document:dom.window.document,IS_REACT_ACT_ENVIRONMENT:true})){
    saved.set(key,Object.getOwnPropertyDescriptor(globalThis,key));Object.defineProperty(globalThis,key,{value,writable:true,configurable:true});
  }
  saved.set('fetch',Object.getOwnPropertyDescriptor(globalThis,'fetch'));
  document.cookie='quicklist_csrf=synthetic-csrf; path=/';
  let enabled=false,synced=false;const calls=[];
  global.fetch=async(url,init={})=>{
    calls.push({url,init});
    if(url.endsWith('/status'))return Response.json({configured:true,connected:true,sync_enabled:enabled,account_email:'synthetic@example.test'});
    if(url.endsWith('/google/sync')){assert.equal(new Headers(init.headers).get('X-CSRF-Token'),'synthetic-csrf');enabled=JSON.parse(init.body).enabled;return Response.json({});}
    if(url.endsWith('/events/synthetic-event/sync')){synced=true;return Response.json({eligible:true,status:'synced',default_duration_minutes:15});}
    if(url.endsWith('/events/synthetic-event'))return Response.json({eligible:true,status:synced?'synced':'not_linked',default_duration_minutes:15});
    assert.fail('Unexpected calendar URL');
  };
  const {createRoot}=require('react-dom/client');const root=createRoot(document.getElementById('root'));
  t.after(async()=>{await React.act(async()=>root.unmount());dom.window.close();saved.forEach((v,k)=>v?Object.defineProperty(globalThis,k,v):delete globalThis[k]);});
  const {CalendarProvider,GoogleCalendarControls,GoogleEventSync}=load('components/google-calendar.tsx');
  await React.act(async()=>root.render(React.createElement(CalendarProvider,null,
    React.createElement(GoogleCalendarControls),React.createElement(GoogleEventSync,{event:{id:'synthetic-event',type:'reminder',title:'Synthetic',event_date:'2026-11-01',event_time:'07:00:00',end_time:null,status:'pending'},disabled:false}))));
  let exportButton=[...document.querySelectorAll('button')].find(b=>b.textContent==='В Google Calendar');
  assert.equal(exportButton.disabled,true);
  await React.act(async()=>document.querySelector('input[type=checkbox]').click());
  exportButton=[...document.querySelectorAll('button')].find(b=>b.textContent==='В Google Calendar');
  assert.equal(exportButton.disabled,false);
  await React.act(async()=>exportButton.click());
  assert.match(document.body.textContent,/синхронизировано/);
  assert.match(document.body.textContent,/Без окончания: 15 мин/);
  assert.equal(calls.filter(c=>c.url.endsWith('/events/synthetic-event/sync')).length,1);
});
