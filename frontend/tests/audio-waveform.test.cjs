const test = require('node:test');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const React = require('react');
const {act} = React;
const {load} = require('./load-component.cjs');

test('waveform follows actual stream samples, stays flat in silence and releases only analyser resources', async t => {
  const dom = new JSDOM('<div id="root"></div>', {url:'http://localhost'});
  const originals = new Map();
  function replace(name,value) {
    originals.set(name,Object.getOwnPropertyDescriptor(globalThis,name));
    Object.defineProperty(globalThis,name,{value,writable:true,configurable:true});
  }
  let frame, signal = 128, connected = 0, disconnected = 0, closed = 0;
  const heights = [];
  const stream = {getTracks:()=>[{stop:()=>assert.fail('visualisation must not stop recording')}]};
  const drawing = {setTransform(){},clearRect(){heights.length=0;},beginPath(){},fill(){},roundRect(x,y,w,h){heights.push(h);}};
  dom.window.HTMLCanvasElement.prototype.getContext = () => drawing;
  dom.window.HTMLElement.prototype.getBoundingClientRect = () => ({width:480,height:58});
  dom.window.matchMedia = () => ({matches:false});
  replace('window',dom.window); replace('document',dom.window.document);
  replace('navigator',dom.window.navigator); replace('IS_REACT_ACT_ENVIRONMENT',true);
  replace('requestAnimationFrame',callback=>{frame=callback;return 1;});
  replace('cancelAnimationFrame',()=>{frame=null;});
  replace('AudioContext',class {
    state='running';
    createMediaStreamSource(value) {assert.equal(value,stream);return {connect(){connected++;},disconnect(){disconnected++;}};}
    createAnalyser() {return {fftSize:2048,getByteTimeDomainData(data){data.fill(signal);},disconnect(){disconnected++;}};}
    resume(){return Promise.resolve();}
    close(){closed++;return Promise.resolve();}
  });
  const root = require('react-dom/client').createRoot(document.getElementById('root'));
  t.after(async()=> {
    await act(async()=>root.unmount()); dom.window.close();
    originals.forEach((value,key)=>value ? Object.defineProperty(globalThis,key,value) : delete globalThis[key]);
  });
  const {AudioWaveform} = load('components/audio-waveform.tsx');
  await act(async()=>root.render(React.createElement(AudioWaveform,{getStream:()=>stream})));
  frame(0); assert.equal(heights.length,48); assert.ok(heights.every(height=>height===2));
  signal = 160; frame(40); assert.ok(heights.every(height=>height>20));
  signal = 128; frame(80); assert.ok(heights.every(height=>height<37));
  assert.equal(connected,1);
  await act(async()=>root.unmount());
  assert.equal(disconnected,2); assert.equal(closed,1); assert.equal(frame,null);
});
