const test = require('node:test');
const assert = require('node:assert/strict');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const {load} = require('./load-component.cjs');
const {UpcomingSidebar,upcomingIcon} = load('components/upcoming-sidebar.tsx');
const {Header} = load('components/header.tsx');

test('sidebar icons use existing type/title with specific video and phone context taking precedence', () => {
  const cases = [
    ['meeting','Зум с коллегами','video'],['meeting','Zoom call','video'],
    ['meeting','Google Meet','video'],['meeting','Онлайн-встреча','video'],
    ['reminder','Позвонить Ивану','phone'],['meeting','Созвон команды','phone'],
    ['reminder','Принять витамины','bell'],['task','Купить билеты','task'],
    ['meeting','Встреча с друзьями','people'],['meeting','Встреча с Иваном','people'],
    ['meeting','Планёрка команды','people'],['meeting','Презентация','calendar'],
    ['unknown','Событие','clock'],
  ];
  for (const [type,title,icon] of cases) assert.equal(upcomingIcon({type,title}),icon,title);
});

test('sidebar keeps real event title/time and end time alongside a subdued outline icon', () => {
  const event={id:'a',type:'meeting',title:'Зум с коллегами',event_date:'2026-10-20',event_time:'09:00:00',end_time:'10:00:00'};
  const html=renderToStaticMarkup(React.createElement(UpcomingSidebar,{events:[event],today:'2026-10-18',disabled:false,onShowPlans(){}}));
  assert.match(html,/Зум с коллегами/); assert.match(html,/09:00/); assert.match(html,/10:00/);
  assert.match(html,/data-event-icon="video"/); assert.match(html,/class="timeline-icon"/);
});

test('visible header brand is QuickList while the existing new conversation action remains', () => {
  const html=renderToStaticMarkup(React.createElement(Header,{status:{text:'Готов к работе',tone:'online'},disabled:false,onNewChat(){}}));
  assert.match(html,/QuickList/); assert.doesNotMatch(html,/GORKYCODE/);
  assert.match(html,/Новый диалог/);
});
