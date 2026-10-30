const test = require('node:test');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const React = require('react');
const {act} = React;
const {load} = require('./load-component.cjs');

async function mount(t, options = {}) {
  const dom = new JSDOM('<div id="root"></div>', {url:'http://localhost:3000', pretendToBeVisual:true});
  const original = new Map();
  function replace(name, value) {
    original.set(name, Object.getOwnPropertyDescriptor(globalThis,name));
    Object.defineProperty(globalThis,name,{value,writable:true,configurable:true});
  }
  const messages = [], speech = [], tracks = [], recorders = [], transcriptions = [], plannerRequests = [];
  const transcripts = options.transcripts || ['Что у меня завтра?'];
  const scrolls = [];
  dom.window.HTMLElement.prototype.scrollIntoView = function() {scrolls.push(this);};
  replace('window',dom.window); replace('document',dom.window.document);
  dom.window.document.cookie='quicklist_csrf=synthetic-csrf; path=/';
  replace('localStorage',dom.window.localStorage); replace('HTMLElement',dom.window.HTMLElement);
  replace('IS_REACT_ACT_ENVIRONMENT',true); replace('isSecureContext',true);
  replace('navigator',{userAgent:'voice-test',mediaDevices:{getUserMedia:async () => {
    if (options.denied) {const error = new Error(); error.name = 'NotAllowedError'; throw error;}
    const track = {stopped:false,stop(){this.stopped=true;},addEventListener(){}}; tracks.push(track);
    return {getTracks:()=>[track]};
  }}});
  class Recorder {
    static isTypeSupported() {return true;}
    constructor(s,o) {this.mimeType=o.mimeType; this.state='inactive'; recorders.push(this);}
    start() {this.state='recording';}
    stop() {this.stopCount=(this.stopCount||0)+1; this.state='inactive'; queueMicrotask(()=> {this.ondataavailable?.({data:new Blob(['recording'])}); this.onstop?.();});}
  }
  replace('MediaRecorder',Recorder);
  replace('Audio',class {pause(){} async play(){} });
  replace('fetch', async (url,init) => {
    if (url === '/api/v1/calendar/google/status') return Response.json({configured:false,connected:false,sync_enabled:false});
    if (url === '/api/v1/events/today') {
      plannerRequests.push(init);
      return Response.json(options.planResponse ? options.planResponse() : {today:'2026-10-28',timezone:'Europe/Moscow',as_of:'2026-10-28T12:00:00+03:00',total_today:0,upcoming:[],without_time:[],overdue_today:[],overdue_earlier:[],ongoing_ids:[],undated_count:0});
    }
    if (url === '/api/v1/conversations') return Response.json([]);
    if (url === '/api/v1/auth/me') return Response.json({id:'voice-test-account'});
    if (url === '/api/health') return Response.json({test_mode:true});
    if (url.endsWith('/transcriptions')) {
      transcriptions.push(init);
      return options.transcriptionResponse ? options.transcriptionResponse() : Response.json({text:transcripts.shift()});
    }
    if (url.endsWith('/speech')) {speech.push(JSON.parse(init.body)); return new Response(new Blob(['audio']));}
    if (url === '/api/v1/assistant/messages') {
      messages.push(JSON.parse(init.body));
      if (options.messageResponse) return options.messageResponse(init);
      return Response.json({conversation_id:'persisted-conversation',message:'На какое время?',action:'CREATE_EVENT',events:[],requires_input:true,missing_fields:['time']});
    }
    assert.fail('Unexpected endpoint '+url);
  });
  // Keep coverage of the existing internal autoSend branch after removing its UI.
  const voiceModule = load('components/use-voice.ts');
  const originalUseVoice = voiceModule.useVoice;
  voiceModule.useVoice = callback => {
    const voice = originalUseVoice(callback);
    React.useEffect(() => {if (options.autoSend) voice.setAutoSend(true);}, []);
    return voice;
  };
  const {createRoot} = require('react-dom/client');
  const root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(load('components/quicklist-app.tsx').default,{account:{id:'voice-test-account',email:'voice@example.test',is_legacy_owner:false}})));
  t.after(async () => {
    await act(async () => root.unmount());
    voiceModule.useVoice = originalUseVoice;
    dom.window.close();
    original.forEach((value,key) => value ? Object.defineProperty(globalThis,key,value) : delete globalThis[key]);
  });
  async function click(label) {
    const button = document.querySelector(`[aria-label="${label}"]`);
    assert.ok(button, label); assert.equal(button.disabled,false,label);
    await act(async () => button.click());
  }
  // Simulate the device ending/automatic limit without explicit submit intent.
  async function record() {await click('Начать голосовой ввод'); await act(async()=>recorders.at(-1).stop());}
  return {messages,speech,tracks,recorders,transcriptions,click,record,dom,scrolls,plannerRequests};
}

test('mounted chat defaults to review mode; transcript waits in composer then uses ordinary Text Core', async t => {
  const ui = await mount(t);
  assert.equal(ui.scrolls.length,0,'empty homepage must not auto-scroll past the responsive header');
  assert.equal(document.querySelector('.auto-speak-control input').checked,false);
  await ui.record();
  assert.equal(document.querySelector('textarea').value,'Что у меня завтра?');
  assert.equal(ui.messages.length,0); assert.equal(ui.speech.length,0);
  assert.ok(ui.tracks.every(track=>track.stopped));
  await ui.click('Отправить сообщение');
  assert.deepEqual(ui.messages,[{conversation_id:null,message:'Что у меня завтра?'}]);
  assert.match(document.body.textContent,/На какое время/);
  assert.equal(ui.speech.length,0);
});

test('fault injection: failed provider releases composer and next explicit send recovers', async t => {
  let attempts=0;
  const ui=await mount(t,{messageResponse:()=>++attempts===1
    ?Response.json({detail:'AI временно недоступен'},{status:503})
    :Response.json({conversation_id:'recovered',message:'Найдено событий: 0.',action:'LIST_EVENTS',events:[],requires_input:false,missing_fields:[]})});
  await ui.record();await ui.click('Отправить сообщение');
  assert.match(document.body.textContent,/AI временно недоступен/);
  assert.equal(document.querySelector('[aria-label="Отправить сообщение"]').disabled,false);
  assert.equal(attempts,1,'failure must not silently replay a paid/mutating request');
  await ui.click('Отправить сообщение');
  assert.equal(attempts,2);
  assert.match(document.body.textContent,/Найдено событий: 0/);
  assert.doesNotMatch(document.body.textContent,/Разбираю запрос/);
});

test('fault injection: slow provider is aborted at the existing deadline and UI becomes usable', async t => {
  const originalTimeout=global.setTimeout;let deadline;
  global.setTimeout=(callback,ms,...args)=>ms===90000?(deadline=callback,originalTimeout(()=>{},0)):originalTimeout(callback,ms,...args);
  t.after(()=>{global.setTimeout=originalTimeout;});
  const ui=await mount(t,{messageResponse:init=>new Promise((resolve,reject)=>{
    const abort=()=>reject(new DOMException('synthetic timeout','AbortError'));
    if(init.signal.aborted)abort();else init.signal.addEventListener('abort',abort,{once:true});
  })});
  await ui.record();await ui.click('Отправить сообщение');
  assert.equal(typeof deadline,'function');
  assert.match(document.body.textContent,/Разбираю запрос/);
  await act(async()=>deadline());
  assert.equal(document.querySelector('[aria-label="Отправить сообщение"]').disabled,false);
  assert.doesNotMatch(document.body.textContent,/Разбираю запрос/);
  assert.equal(ui.messages.length,1,'timeout must not create an automatic retry');
});
test('explicit auto mode carries conversation ID through voice followups; auto speech only reads server reply', async t => {
  const ui = await mount(t,{autoSend:true,transcripts:['Напомни завтра позвонить Диме','В 15:00']});
  const autoSpeak = document.querySelector('.auto-speak-control input');
  await act(async()=>autoSpeak.click());
  await ui.record(); await ui.record();
  assert.deepEqual(ui.messages,[{conversation_id:null,message:'Напомни завтра позвонить Диме'},
    {conversation_id:'persisted-conversation',message:'В 15:00'}]);
  assert.deepEqual(ui.speech,[{text:'На какое время?'},{text:'На какое время?'}]);
});
test('existing draft survives recognition and prevents automatic sending of combined content', async t => {
  const ui = await mount(t,{autoSend:true});
  const example = [...document.querySelectorAll('button')].find(button=>button.textContent.includes('позвонить Ивану'));
  await act(async()=>example.click());
  const draft = document.querySelector('textarea').value;
  await ui.record();
  assert.equal(document.querySelector('textarea').value,draft+'\nЧто у меня завтра?');
  assert.equal(ui.messages.length,0);
});
test('denied microphone shows Russian error and leaves text and keyboard submission available', async t => {
  const ui = await mount(t,{denied:true});
  await ui.click('Начать голосовой ввод');
  assert.match(document.querySelector('.voice-feedback').textContent,/Доступ к микрофону запрещён/);
  assert.equal(document.querySelector('textarea').disabled,false);
  assert.equal(ui.messages.length,0);
});
test('new chat cancels active recording without uploading or sending', async t => {
  const ui = await mount(t);
  await ui.click('Начать голосовой ввод');
  assert.equal(document.querySelector('textarea').disabled,true);
  const newChat = [...document.querySelectorAll('button')].find(button=>button.textContent.includes('Новый диалог'));
  assert.ok(newChat);
  await act(async()=>newChat.click());
  assert.equal(ui.messages.length,0); assert.ok(ui.tracks.every(track=>track.stopped));
  assert.equal(document.querySelector('textarea').disabled,false);
});
test('turning off auto speech during Text Core request prevents a late paid TTS request', async t => {
  let resolve;
  const ui = await mount(t,{autoSend:true,messageResponse:()=>new Promise(r=>resolve=r)});
  const autoSpeak = document.querySelector('.auto-speak-control input');
  await act(async()=>autoSpeak.click());
  await ui.record();
  assert.equal(ui.messages.length,1);
  await act(async()=>autoSpeak.click());
  await act(async()=>resolve(Response.json({conversation_id:'persisted-conversation',message:'Найдено событий: 0.',action:'LIST_EVENTS',events:[],requires_input:false,missing_fields:[]})));
  assert.equal(ui.speech.length,0);
});

for (const autoSend of [false,true]) test(`airplane explicitly sends voice once with autoSend=${autoSend}, preserving old draft`, async t => {
  const ui = await mount(t,{autoSend});
  const example = [...document.querySelectorAll('button')].find(button=>button.textContent.includes('позвонить Ивану'));
  await act(async()=>example.click());
  const draft = document.querySelector('textarea').value;
  await ui.click('Начать голосовой ввод');
  const plane = document.querySelector('[aria-label="Отправить голос"]');
  assert.equal(plane.disabled,false);
  assert.equal(document.querySelector('[aria-label="Отменить запись"]').disabled,false);
  assert.equal(document.querySelectorAll('.voice-feedback button').length,0);
  await act(async()=>{plane.click();plane.click();plane.click();});
  await act(async()=>ui.recorders[0].onstop());
  assert.equal(ui.recorders[0].stopCount,1);
  assert.equal(ui.transcriptions.length,1);
  assert.deepEqual(ui.messages,[{conversation_id:null,message:'Что у меня завтра?'}]);
  assert.equal(document.querySelector('textarea').value,draft);
});

test('square cancel ignores final callbacks and preserves textarea without any requests', async t => {
  const ui = await mount(t);
  const example = document.querySelector('.example-chip');
  await act(async()=>example.click()); const draft=document.querySelector('textarea').value;
  await ui.click('Начать голосовой ввод');
  const stop=ui.recorders[0].onstop;
  await ui.click('Отменить запись'); await act(async()=>stop());
  assert.equal(document.querySelector('textarea').value,draft);
  assert.equal(document.querySelector('textarea').disabled,false);
  assert.equal(ui.transcriptions.length,0); assert.equal(ui.messages.length,0);
  assert.ok(ui.tracks.every(track=>track.stopped));
  assert.equal(document.querySelector('.audio-waveform'),null);
});

test('processing blocks second recording/send; cancellation aborts STT and ignores its late answer', async t => {
  let resolve;
  const ui=await mount(t,{transcriptionResponse:()=>new Promise(r=>resolve=r)});
  await ui.click('Начать голосовой ввод'); await ui.click('Отправить голос');
  assert.equal(document.querySelector('.send-button').disabled,true);
  assert.equal(document.querySelector('[aria-label="Начать голосовой ввод"]'),null);
  await ui.click('Отменить запись или распознавание');
  assert.equal(ui.transcriptions[0].signal.aborted,true);
  await act(async()=>resolve(Response.json({text:'Поздняя запись'})));
  assert.equal(ui.messages.length,0); assert.equal(document.querySelector('textarea').value,'');
});

test('only approved auto-TTS capsule remains and its choice survives new chat', async t => {
  await mount(t);
  const capsule=document.querySelector('.auto-speak-control');
  assert.ok(capsule.closest('form.composer'));
  assert.equal(document.querySelectorAll('input[type=checkbox]').length,1);
  assert.doesNotMatch(document.body.textContent,/Настройки голоса|Отправлять после распознавания/);
  const speak=capsule.querySelector('input');
  assert.equal(speak.checked,false);
  await act(async()=>speak.click());
  const newChat=document.querySelector('.new-chat-button');
  await act(async()=>{newChat.click();newChat.click();newChat.click();});
  assert.equal(speak.checked,true);
  assert.match(document.querySelector('.brand').textContent,/QuickList/);
  assert.doesNotMatch(document.body.textContent,/GORKYCODE|Обычными словами/);
});

for (const action of ['CREATE_EVENT','UPDATE_EVENT','COMPLETE_EVENT','DELETE_EVENT']) test(`account planner refreshes after ${action} independently of response snapshots`,async t=>{
  let version=0;
  const ui=await mount(t,{
    planResponse:()=>({today:'2026-10-28',timezone:'Europe/Moscow',as_of:'2026-10-28T12:00:00+03:00',total_today:version,
      upcoming:version?[{id:'outside-chat',type:'task',title:'Account outside chat',event_date:'2026-10-28',event_time:'18:00:00',end_time:null,status:'pending'}]:[],
      without_time:[],overdue_today:[],overdue_earlier:[],ongoing_ids:[],undated_count:0}),
    messageResponse:()=>{version=1;return Response.json({conversation_id:'persisted',message:'Done',action,events:[],requires_input:false,missing_fields:[]});},
  });
  await act(async()=>document.querySelector('.example-chip').click());
  await ui.click('Отправить сообщение');
  assert.equal(ui.plannerRequests.length,2);
  assert.match(document.querySelector('.sidebar-desktop').textContent,/Account outside chat/);
  assert.equal(document.querySelector('.sidebar-desktop h2>span').textContent,'1');
});
