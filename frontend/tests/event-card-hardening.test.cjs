const assert = require('node:assert/strict');
const test = require('node:test');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const {JSDOM} = require('jsdom');
const {load} = require('./load-component.cjs');
const {EventCard} = load('components/event-card.tsx');
const base = {id:'qa', type:'meeting', title:'с Иваном', event_date:'2026-10-17', event_time:null,
  end_time:null, status:'pending', source:'user', type_inferred:false};
function card(changes={}) {
  const html = renderToStaticMarkup(React.createElement(EventCard, {event:{...base,...changes},onAction(){}}));
  return new JSDOM(html).window.document;
}
for (const type of ['meeting','task','reminder']) test(`${type} absent clock is secondary metadata, never a clock heading`, () => {
  const doc = card({type});
  assert.equal(doc.querySelector('.event-time'), null);
  assert.equal(doc.querySelector('.event-title').textContent, base.title);
  assert.ok(doc.querySelector('.event-schedule').textContent.includes('Время не указано'));
  assert.equal(doc.querySelectorAll('button').length, 2);
  assert.ok(!doc.body.textContent.includes('00:00'));
});
test('unscheduled completed meeting retains title/date/held badge/delete without fake time', () => {
  const doc = card({status:'completed'});
  assert.equal(doc.querySelector('.event-title').textContent, base.title);
  assert.equal(doc.querySelector('time').dateTime, base.event_date);
  assert.equal(doc.querySelector('[role=status]').textContent,'Состоялась');
  assert.equal(doc.querySelectorAll('button').length,1);
});
test('long literal user title is preserved without database-style rewriting', () => {
  const title = 'ОченьДлинноеНазваниеБезПробелов'.repeat(12);
  assert.equal(card({title}).querySelector('.event-title').textContent,title);
});
test('timed and overnight meeting retain clocks and make next-day ending visible', () => {
  assert.equal(card({event_time:'19:00:00',end_time:'21:00:00'}).querySelector('.event-time').textContent,'19:00–21:00');
  assert.ok(card({event_time:'23:00:00',end_time:'01:00:00'}).querySelector('.event-time').textContent.includes('+1 день'));
});
