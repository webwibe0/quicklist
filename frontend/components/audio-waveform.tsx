import { useEffect, useRef } from "react";

/** UI-only analyser of the existing stream: no second permission, recording or upload. */
export function AudioWaveform({getStream}: {getStream: () => MediaStream | undefined}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const streamReader = useRef(getStream); streamReader.current = getStream;
  useEffect(() => {
    const stream = streamReader.current();
    const element = canvas.current;
    if (!stream || !element || typeof AudioContext === "undefined") return;
    let context: AudioContext | undefined;
    let source: MediaStreamAudioSourceNode | undefined;
    let analyser: AnalyserNode | undefined;
    let animation = 0;
    let stopped = false;
    try {
      const drawing = element.getContext("2d");
      if (!drawing) return;
      context = new AudioContext();
      source = context.createMediaStreamSource(stream);
      analyser = context.createAnalyser();
      analyser.fftSize = 2048;
      source.connect(analyser); // Never connect to speakers.
      void context.resume().catch(() => {});
      const data = new Uint8Array(analyser.fftSize);
      const heights = new Float32Array(48);
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
      let previous = -Infinity;
      function draw(now: number) {
        if (stopped || !analyser || !element || !drawing) return;
        animation = requestAnimationFrame(draw);
        if (now - previous < (reduced.matches ? 150 : 32)) return;
        previous = now;
        const {width, height} = element.getBoundingClientRect();
        const ratio = Math.min(window.devicePixelRatio || 1, 2);
        const w = Math.round(width * ratio), h = Math.round(height * ratio);
        if (element.width !== w || element.height !== h) {element.width = w; element.height = h;}
        drawing.setTransform(ratio, 0, 0, ratio, 0, 0);
        drawing.clearRect(0, 0, width, height);
        analyser.getByteTimeDomainData(data);
        let energy = 0;
        const step = width / heights.length;
        for (let bar = 0; bar < heights.length; bar++) {
          let sum = 0;
          const start = Math.floor(bar * data.length / heights.length);
          const end = Math.floor((bar + 1) * data.length / heights.length);
          for (let i = start; i < end; i++) sum += ((data[i] - 128) / 128) ** 2;
          const rms = Math.sqrt(sum / (end - start));
          const target = rms < .008 ? 0 : Math.min(1, rms * 5);
          heights[bar] += (target - heights[bar]) * (target > heights[bar] ? .62 : .22);
          energy += heights[bar];
          const amplitude = 2 + heights[bar] * (height - 6);
          drawing.fillStyle = `rgba(218, 232, 186, ${.38 + heights[bar] * .6})`;
          drawing.beginPath();
          drawing.roundRect(bar * step + step * .28, (height - amplitude) / 2, Math.max(2, step * .35), amplitude, 3);
          drawing.fill();
        }
        element.style.setProperty("--voice-energy", String(energy / heights.length));
      }
      animation = requestAnimationFrame(draw);
    } catch { /* A visual meter must never interrupt capture or the text fallback. */ }
    return () => {
      stopped = true; cancelAnimationFrame(animation);
      source?.disconnect(); analyser?.disconnect();
      if (context && context.state !== "closed") void context.close().catch(() => {});
      // The recording controller owns the MediaStream and stops its tracks.
    };
  }, []);
  return <canvas className="audio-waveform" ref={canvas} aria-label="Уровень сигнала микрофона" role="img" />;
}
