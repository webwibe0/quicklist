const test = require('node:test');
const assert = require('node:assert/strict');
const {load} = require('./load-component.cjs');
const {apiFetch, csrfCookie} = load('lib/api.ts');
const {restoreHistory} = load('lib/chat-state.ts');

test('aborting a message also aborts a stalled CSRF bootstrap before any mutation', async t => {
  const oldDocument = global.document;
  const oldFetch = global.fetch;
  global.document = {cookie:''};
  const calls = [];
  const controller = new AbortController();
  global.fetch = (url, init) => {
    calls.push({url,init});
    return new Promise((resolve, reject) => {
      init.signal?.addEventListener('abort', () => reject(new DOMException('aborted','AbortError')), {once:true});
    });
  };
  t.after(() => {global.document=oldDocument;global.fetch=oldFetch;});
  const request = apiFetch('/api/v1/assistant/messages',{method:'POST',body:'{}',signal:controller.signal});
  // Inspect the actual stalled request, not a mirrored implementation check.
  assert.equal(calls.length,1);
  const rejected = assert.rejects(request,{name:'AbortError'});
  controller.abort();
  await Promise.race([rejected,new Promise((_,reject)=>setTimeout(()=>reject(new Error('CSRF bootstrap ignored cancellation')),50))]);
  assert.equal(calls.length,1);
});

test('mutation carries server-issued CSRF cookie and same-origin credentials', async t => {
  const oldDocument = global.document;
  const oldFetch = global.fetch;
  global.document = {cookie:'quicklist_csrf=local-token; __Host-quicklist_csrf=secure-token'};
  const calls = [];
  global.fetch = async (url, init) => {calls.push({url,init}); return Response.json({ok:true});};
  t.after(() => {global.document=oldDocument;global.fetch=oldFetch;});
  assert.equal(csrfCookie(),'secure-token');
  await apiFetch('/api/v1/voice/speech',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  assert.equal(calls.length,1);
  assert.equal(calls[0].init.headers.get('X-CSRF-Token'),'secure-token');
  assert.equal(calls[0].init.credentials,'same-origin');
  assert.equal(calls[0].init.cache,'no-store');
});

test('server history restores ordinary messages without a shared browser transcript', () => {
  assert.deepEqual(restoreHistory([{role:'user',text:'A'},{role:'assistant',text:'B'}]),[
    {role:'user',text:'A'},{role:'assistant',text:'B'}]);
});

test('logout remains available when chat history is unavailable', () => {
  const React = require('react');
  const {renderToStaticMarkup} = require('react-dom/server');
  const {JSDOM} = require('jsdom');
  const {Header} = load('components/header.tsx');
  const html = renderToStaticMarkup(React.createElement(Header, {
    status:{text:'Сервер недоступен',tone:'offline'}, disabled:true,
    account:{email:'synthetic@example.test'}, onLogout(){}, onNewChat(){},
  }));
  const document = new JSDOM(html).window.document;
  assert.equal(document.querySelector('.account-controls button').disabled,false);
  assert.equal(document.querySelector('.new-chat-button').disabled,true);
});

test('account header clearly exposes unverified email without removing owner access', () => {
  const React = require('react');
  const {renderToStaticMarkup} = require('react-dom/server');
  const {Header} = load('components/header.tsx');
  const html=renderToStaticMarkup(React.createElement(Header,{
    status:{text:'Ready',tone:'online'},disabled:false,onNewChat(){},onLogout(){},
    account:{email:'owner@example.test',email_verified:false},
  }));
  assert.match(html,/href="\/verify-email"/);
  assert.match(html,/Подтвердить email/);
});
