const test = require('node:test');
const assert = require('node:assert/strict');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const {JSDOM} = require('jsdom');
const {load} = require('./load-component.cjs');

test('approved header groups actual readiness under dynamic date, logo and real account', () => {
  const {Header} = load('components/header.tsx');
  const doc = new JSDOM(renderToStaticMarkup(React.createElement(Header, {
    status:{text:'Сервер недоступен',tone:'offline'},today:'2026-10-28',disabled:false,
    onNewChat(){},onLogout(){},account:{email:'alice@example.test',email_verified:false},
  }))).window.document;
  assert.equal(doc.querySelector('.header-day .connection-status').textContent,'Сервер недоступен');
  assert.ok(doc.querySelector('.brand-mark svg[data-quicklist-logo]'));
  assert.equal(doc.querySelector('time').dateTime,'2026-10-28');
  assert.equal(doc.querySelector('.profile-monogram').textContent,'AL');
  assert.match(doc.querySelector('.account-controls').textContent,/alice@example.test/);
  assert.ok(doc.querySelector('a[href="/verify-email"]'));
});

test('approved composer has exactly one real auto-TTS switch and no former voice settings', () => {
  const {Composer} = load('components/composer.tsx');
  const voice = {recording:{kind:'idle',seconds:0,message:''},autoSend:false,autoSpeak:false};
  const doc = new JSDOM(renderToStaticMarkup(React.createElement(Composer, {
    value:'',onChange(){},onSubmit(){},inputRef:{current:null},busy:false,ready:true,voice,
  }))).window.document;
  const control = doc.querySelector('.auto-speak-control');
  assert.ok(control.closest('form.composer'));
  assert.equal(doc.querySelectorAll('input[type=checkbox]').length,1);
  assert.equal(control.querySelector('input').checked,false);
  assert.match(control.textContent,/Озвучивать ответы автоматически/);
  assert.doesNotMatch(doc.body.textContent,/Настройки голоса|Отправлять после распознавания/);
  assert.ok(doc.querySelector('[aria-label="Начать голосовой ввод"]'));
  assert.ok(doc.querySelector('[aria-label="Отправить сообщение"]'));
});

test('calendar presentation separates OAuth connection from sync and uncertainty', () => {
  const {calendarBadge} = load('components/google-calendar.tsx');
  const off = calendarBadge({configured:true,connected:true,sync_enabled:false});
  const on = calendarBadge({configured:true,connected:true,sync_enabled:true});
  assert.equal(off.label,'подключён');
  assert.match(off.description,/выключена/);
  assert.match(on.description,/включена/);
  assert.equal(calendarBadge({configured:true,connected:false,sync_enabled:false}).label,'не подключён');
  assert.equal(calendarBadge(null).tone,'waiting');
  assert.equal(calendarBadge(null,true).tone,'error');
  assert.equal(calendarBadge({configured:true,connected:true,sync_enabled:false,error_code:'REAUTH_REQUIRED'}).tone,'error');
});
