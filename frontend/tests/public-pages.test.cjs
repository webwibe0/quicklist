const test = require('node:test');
const assert = require('node:assert/strict');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const {JSDOM} = require('jsdom');
const {load} = require('./load-component.cjs');
const {PublicHome,PrivacyPage,TermsPage} = load('components/public-pages.tsx');

test('anonymous home explains the real product and links to login, registration and privacy',()=>{
  const doc=new JSDOM(renderToStaticMarkup(React.createElement(PublicHome))).window.document;
  assert.match(doc.querySelector('h1').textContent,/Скажите как есть/);
  for(const href of ['/login','/register','/privacy','/terms']) assert.ok(doc.querySelector(`a[href="${href}"]`));
  assert.match(doc.body.textContent,/Google Calendar/);
  assert.equal(doc.querySelectorAll('form,input,textarea').length,0);
});

test('privacy accurately separates soft-delete, disconnect, revocation and backup retention',()=>{
  const doc=new JSDOM(renderToStaticMarkup(React.createElement(PrivacyPage))).window.document;
  for(const text of ['OAuth','OpenAI','Resend','резервн','Google Calendar','не удаляет','физического лица']) assert.ok(doc.body.textContent.includes(text),text);
  assert.ok(doc.querySelector('a[href="mailto:inferno.32222@gmail.com"]'));
  assert.doesNotMatch(doc.body.textContent,/предварительная редакция|support@quicklist-ai.com|GDPR|ISO 27001/);
  assert.match(doc.body.textContent,/не передаются в OpenAI/);
});

test('educational terms remain public and do not pretend to sell subscriptions or Google verification',()=>{
  const doc=new JSDOM(renderToStaticMarkup(React.createElement(TermsPage))).window.document;
  assert.match(doc.body.textContent,/учебный проект/);
  assert.match(doc.body.textContent,/не подключены/);
  assert.ok(doc.querySelector('a[href="/privacy"]'));
});

test('public branding uses the exact configured app name and clearly excludes image generation',()=>{
  const doc=new JSDOM(renderToStaticMarkup(React.createElement(PublicHome))).window.document;
  assert.equal(doc.querySelector('.public-brand span').firstChild.textContent,'QuickList');
  assert.match(doc.body.textContent,/QuickList помогает/);
  assert.match(doc.body.textContent,/изображения и видео/);
});

test('Google disclosures link the official data policy and explain Limited Use without claiming verification',()=>{
  const doc=new JSDOM(renderToStaticMarkup(React.createElement(PrivacyPage))).window.document;
  assert.ok(doc.querySelector('a[href="https://developers.google.com/terms/api-services-user-data-policy"]'));
  assert.ok(doc.querySelector('a[href="https://developers.google.com/terms"]'));
  assert.match(doc.body.textContent,/Limited Use/);
  assert.match(doc.body.textContent,/отдельного согласия/);
});
