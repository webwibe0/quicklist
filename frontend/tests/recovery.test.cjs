const test = require('node:test');
const assert = require('node:assert/strict');
const React = require('react');
const {JSDOM} = require('jsdom');
const {load} = require('./load-component.cjs');

test('reset UI removes URL fragment, masks both passwords and rejects mismatch before network', async t => {
  const dom = new JSDOM('<div id="root"></div>', {url:'https://example.test/reset-password#token='+'a'.repeat(43)});
  const originals=new Map();
  for (const [key,value] of Object.entries({window:dom.window,document:dom.window.document,
    HTMLElement:dom.window.HTMLElement,IS_REACT_ACT_ENVIRONMENT:true})) {
    originals.set(key,Object.getOwnPropertyDescriptor(global,key));
    Object.defineProperty(global,key,{value,writable:true,configurable:true});
  }
  const originalFetch=global.fetch;const calls=[];
  global.fetch=async (...args)=>{calls.push(args);throw Error('Unexpected network call');};
  const {createRoot}=require('react-dom/client');
  const root=createRoot(document.getElementById('root'));
  t.after(async()=>{
    await React.act(async()=>root.unmount());global.fetch=originalFetch;dom.window.close();
    for(const [key,descriptor] of originals) descriptor?Object.defineProperty(global,key,descriptor):delete global[key];
  });
  await React.act(async()=>root.render(React.createElement(load('components/account-recovery.tsx').AccountRecovery,{mode:'reset-password'})));
  assert.equal(window.location.hash,'');
  const fields=[...document.querySelectorAll('input')];
  assert.equal(fields.length,2);assert.ok(fields.every(input=>input.type==='password'&&input.autocomplete==='new-password'));
  // Empty fields submitted programmatically exercise the explicit mismatch guard.
  const setter=Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype,'value').set;
  await React.act(async()=>{
    setter.call(fields[0],'Synthetic-example-123');fields[0].dispatchEvent(new dom.window.Event('input',{bubbles:true}));
    setter.call(fields[1],'Different-example-456');fields[1].dispatchEvent(new dom.window.Event('input',{bubbles:true}));
  });
  await React.act(async()=>document.querySelector('form').dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true})));
  assert.match(document.querySelector('[role="alert"]').textContent,/не совпадают/);
  assert.equal(calls.length,0);assert.equal(document.querySelector('button').disabled,false);
});
