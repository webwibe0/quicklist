const test = require('node:test');
const assert = require('node:assert/strict');
const React = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
const {load} = require('./load-component.cjs');
const {VoiceRecorder, VoicePlayer, speechText, idleRecording, idlePlayback} = load('lib/voice.ts');
const {VoiceButton} = load('components/voice-button.tsx');
const {SpeechButton} = load('components/speech-button.tsx');
const tick = () => new Promise(resolve => setImmediate(resolve));

function environment(t, options = {}) {
  const originals = new Map();
  function replace(name, value) {
    originals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, {value, configurable: true, writable: true});
  }
  const tracks = [{stopCount: 0, stop() {this.stopCount++;}, addEventListener() {}}];
  const stream = {getTracks: () => tracks};
  const recordings = []; const requests = []; const audios = [];
  class Recorder {
    static isTypeSupported(type) {return options.mime ? type === options.mime : true;}
    constructor(stream, opts) {this.mimeType = opts.mimeType; this.state = 'inactive'; recordings.push(this);}
    start() {this.state = 'recording';}
    stop() {
      this.state = 'inactive';
      queueMicrotask(() => {this.ondataavailable?.({data: new Blob(options.empty ? [] : ['audio'])}); this.onstop?.();});
    }
  }
  class Audio {
    constructor(url) {this.src = url; this.currentTime = 0; audios.push(this); this.paused = true;}
    pause() {this.paused = true;}
    async play() {if (options.blocked) {const error = new Error(); error.name = 'NotAllowedError'; throw error;} this.paused = false;}
  }
  replace('isSecureContext', true); replace('MediaRecorder', Recorder); replace('Audio', Audio);
  replace('navigator', {mediaDevices: {getUserMedia: options.permission || (async () => stream)}});
  replace('fetch', async (url, init) => {
    requests.push({url, init});
    if (options.fetch) return options.fetch(url, init);
    return url.endsWith('transcriptions') ? Response.json({text: 'Что у меня завтра?'})
      : new Response(new Blob(['audio'], {type: 'audio/mpeg'}));
  });
  t.after(() => originals.forEach((descriptor, name) => descriptor ? Object.defineProperty(globalThis, name, descriptor) : delete globalThis[name]));
  return {tracks, stream, recordings, requests, audios, options};
}

test('record, stop, release microphone, return STT text; repeated start/stop only uploads once', async t => {
  const env = environment(t); const transcripts = [];
  const recorder = new VoiceRecorder(() => {}, text => transcripts.push(text)); t.after(() => recorder.cancel());
  await Promise.all([recorder.start(), recorder.start()]);
  assert.equal(env.recordings.length, 1); assert.equal(recorder.state.kind, 'recording');
  recorder.stop(); recorder.stop(); await tick();
  assert.deepEqual(transcripts, ['Что у меня завтра?']);
  assert.equal(env.requests.length, 1); assert.ok(env.tracks[0].stopCount > 0);
  assert.equal(recorder.state.kind, 'done');
});
test('Safari MP4 selected when WebM/Opus unsupported', async t => {
  const env = environment(t, {mime: 'audio/mp4'});
  const recorder = new VoiceRecorder(() => {}, () => {}); t.after(() => recorder.cancel());
  await recorder.start(); recorder.stop(); await tick();
  assert.equal(new Headers(env.requests[0].init.headers).get('Content-Type'), 'audio/mp4');
});

test('explicit voice submission carries intent once despite repeated onstop callbacks', async t => {
  const env = environment(t); const transcripts = [];
  const recorder = new VoiceRecorder(() => {}, (text, submit) => transcripts.push({text, submit}));
  t.after(() => recorder.cancel());
  await recorder.start();
  const stopped = env.recordings[0].onstop;
  recorder.stop(true); recorder.stop(true); await tick();
  stopped(); await tick();
  assert.equal(env.requests.length, 1);
  assert.deepEqual(transcripts, [{text:'Что у меня завтра?', submit:true}]);
});

test('square cancellation discards buffered data and late final callbacks without STT', async t => {
  const env = environment(t);
  const recorder = new VoiceRecorder(() => {}, () => assert.fail('cancelled transcript'));
  await recorder.start();
  const media = env.recordings[0], stopped = media.onstop, data = media.ondataavailable;
  data({data:new Blob(['buffered'])});
  recorder.cancel(); data({data:new Blob(['late'])}); stopped(); await tick();
  assert.equal(env.requests.length,0); assert.equal(recorder.state.kind,'idle');
  assert.equal(recorder.getStream(),undefined); assert.ok(env.tracks[0].stopCount > 0);
});
test('recording automatically stops below the server 60-second limit', async t => {
  const env = environment(t);
  t.mock.timers.enable({apis:['setTimeout','setInterval','Date']});
  const recorder = new VoiceRecorder(() => {}, () => {}); t.after(() => recorder.cancel());
  await recorder.start(); t.mock.timers.tick(59000); await tick();
  assert.equal(env.recordings[0].state, 'inactive');
  assert.equal(env.requests.length, 1); assert.ok(env.tracks[0].stopCount > 0);
});
test('permission denial gives actionable error and makes no request', async t => {
  const error = new Error(); error.name = 'NotAllowedError';
  const env = environment(t, {permission: async () => {throw error;}});
  const recorder = new VoiceRecorder(() => {}, () => assert.fail('no transcript'));
  await recorder.start(); assert.equal(recorder.state.kind, 'error');
  assert.match(recorder.state.message, /Доступ.*запрещён/); assert.equal(env.requests.length, 0);
});
test('cancel pending permission releases late microphone without starting recording', async t => {
  let resolve; const permission = new Promise(r => resolve = r);
  const env = environment(t, {permission: () => permission});
  const recorder = new VoiceRecorder(() => {}, () => assert.fail('late transcript'));
  const starting = recorder.start(); recorder.cancel(); resolve(env.stream); await starting;
  assert.equal(env.recordings.length, 0); assert.equal(env.tracks[0].stopCount, 1);
});
test('empty recording never uploads', async t => {
  const env = environment(t, {empty: true});
  const recorder = new VoiceRecorder(() => {}, () => assert.fail('empty transcript')); t.after(() => recorder.cancel());
  await recorder.start(); recorder.stop(); await tick();
  assert.equal(recorder.state.kind, 'error'); assert.equal(env.requests.length, 0);
});
test('STT failure releases microphone, reports error, and permits another recording', async t => {
  const env = environment(t, {fetch: async () => Response.json({detail: 'Речь не слышна.'}, {status: 422})});
  const recorder = new VoiceRecorder(() => {}, () => assert.fail('no transcript')); t.after(() => recorder.cancel());
  await recorder.start(); recorder.stop(); await tick();
  assert.equal(recorder.state.kind, 'error'); assert.equal(recorder.state.message, 'Речь не слышна.');
  await recorder.start(); assert.equal(recorder.state.kind, 'recording'); assert.equal(env.recordings.length, 2);
});
test('cancel upload aborts request and discards even a late response', async t => {
  let resolve;
  const env = environment(t, {fetch: () => new Promise(r => resolve = r)});
  const recorder = new VoiceRecorder(() => {}, () => assert.fail('stale transcript')); t.after(() => recorder.cancel());
  await recorder.start(); recorder.stop(); await tick(); recorder.cancel();
  assert.equal(env.requests[0].init.signal.aborted, true);
  resolve(Response.json({text: 'Удалить всё'})); await tick(); assert.equal(recorder.state.kind, 'idle');
});
test('TTS stop pauses and replay does not spend another request', async t => {
  const env = environment(t); const player = new VoicePlayer(() => {}); t.after(() => player.dispose());
  await Promise.all([player.play('one', 'Реальный ответ'), player.play('one', 'Реальный ответ')]);
  assert.equal(player.state.kind, 'playing'); assert.equal(env.requests.length, 1);
  assert.deepEqual(JSON.parse(env.requests[0].init.body), {text: 'Реальный ответ'});
  player.stop(); assert.equal(env.audios[0].paused, true);
  await player.play('one', 'Реальный ответ'); assert.equal(env.requests.length, 1);
  env.audios[0].onended(); assert.equal(player.state.kind, 'idle');
});
test('autoplay rejection offers Play and reuses downloaded audio', async t => {
  const env = environment(t, {blocked: true}); const player = new VoicePlayer(() => {}); t.after(() => player.dispose());
  await player.play('one', 'Ответ'); assert.equal(player.state.kind, 'blocked');
  env.options.blocked = false; await player.play('one', 'Ответ');
  assert.equal(player.state.kind, 'playing'); assert.equal(env.requests.length, 1);
});
test('TTS failure leaves a recoverable error', async t => {
  environment(t, {fetch: async () => Response.json({detail: 'Озвучивание недоступно.'}, {status: 503})});
  const player = new VoicePlayer(() => {}); t.after(() => player.dispose());
  await player.play('one', 'Ответ'); assert.equal(player.state.kind, 'error');
  assert.equal(player.state.message, 'Озвучивание недоступно.');
});
test('cancel generation prevents late playback', async t => {
  let resolve; const env = environment(t, {fetch: () => new Promise(r => resolve = r)});
  const player = new VoicePlayer(() => {}); t.after(() => player.dispose());
  const pending = player.play('one', 'Ответ'); player.stop();
  resolve(new Response(new Blob(['audio']))); await pending;
  assert.equal(env.audios.length, 0); assert.equal(env.requests[0].init.signal.aborted, true);
});
test('speech only uses backend text, preserves confirmations and summarizes candidate lists deterministically', () => {
  assert.equal(speechText({role: 'assistant', text: 'Найдено событий: 50.', events: Array(50).fill({title:'Secret'})}), 'Найдено событий: 50.');
  assert.equal(speechText({role:'assistant', text:'Удалить встречу? Ответьте да или нет.', missing_fields:['confirmation']}), 'Удалить встречу? Ответьте да или нет.');
  assert.match(speechText({role:'assistant', text:'Уточните номер:\n1. Запись', missing_fields:['event_reference']}), /Варианты показаны/);
  assert.equal(speechText({role:'user', text:'Не читать'}), '');
  assert.equal(speechText({role:'assistant', text:'a'.repeat(2401)}), '');
});
test('recording/mobile controls expose cancellation, timer and accessible playback status', () => {
  const voice = {recording:{...idleRecording,kind:'recording',seconds:7},cancel(){}};
  const html = renderToStaticMarkup(React.createElement(VoiceButton, {voice}));
  assert.match(html, /Отменить запись/); assert.match(html, /0:07/); assert.match(html, /aria-pressed="true"/);
  const speech = renderToStaticMarkup(React.createElement(SpeechButton, {id:'1',state:{...idlePlayback,id:'1',kind:'blocked',message:'Нажмите Play'},disabled:false}));
  assert.match(speech, /Воспроизвести ответ/); assert.match(speech, /role="status"/);
});
