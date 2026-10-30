import { apiFetch } from "./api";
import type { ChatEntry } from "@/types/assistant";

export type RecordingState = {kind: "idle" | "permission" | "recording" | "processing" | "done" | "error"; seconds: number; message: string; submitted?: boolean};
export type PlaybackState = {kind: "idle" | "loading" | "playing" | "blocked" | "error"; id: string; message: string};
export const idleRecording: RecordingState = {kind: "idle", seconds: 0, message: ""};
export const idlePlayback: PlaybackState = {kind: "idle", id: "", message: ""};
export const recordingBusy = (state: RecordingState) => ["permission", "recording", "processing"].includes(state.kind);
const MIME_TYPES = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus", "audio/webm"];
const MAX_BYTES = 8 * 1024 * 1024;

export function speechText(entry: Pick<ChatEntry, "text" | "role" | "missing_fields">): string {
  if (entry.role !== "assistant") return "";
  // Candidate lists can be long. Preserve the real introduction and direct the user to the visible list.
  const text = entry.missing_fields?.includes("event_reference") && entry.text.includes("\n")
    ? `${entry.text.split("\n")[0]} Варианты показаны в сообщении.` : entry.text;
  // Never cut a confirmation in half or invent a shorter interpretation.
  return text.length <= 2400 ? text : "";
}

async function checked(response: Response, fallback: string) {
  if (response.ok) return response;
  const body = await response.json().catch(() => null);
  throw new Error(typeof body?.detail === "string" ? body.detail : fallback);
}

export function microphoneError(error: unknown): string {
  const name = error instanceof Error ? error.name : "";
  if (name === "NotAllowedError" || name === "SecurityError") return "Доступ к микрофону запрещён. Разрешите его в настройках сайта или напишите текстом.";
  if (name === "NotFoundError") return "Микрофон не найден. Подключите его или напишите текстом.";
  if (name === "NotReadableError") return "Микрофон занят другим приложением. Освободите его и повторите.";
  return "Не удалось записать звук. Попробуйте ещё раз или напишите текстом.";
}

/** Owns one recording and its upload. Generation guards discard late permission/network results. */
export class VoiceRecorder {
  state = idleRecording;
  private generation = 0;
  private recorder?: MediaRecorder;
  private stream?: MediaStream;
  private timer?: ReturnType<typeof setInterval>;
  private limit?: ReturnType<typeof setTimeout>;
  private request?: AbortController;
  private audio?: {chunks: Blob[]; bytes: number; finalized: boolean; submit: boolean};
  constructor(private update: (state: RecordingState) => void, private transcript: (text: string, submit: boolean) => void) {}
  // Read-only tap for UI metering; recording/upload behavior stays in this controller.
  getStream() { return this.stream; }
  private set(kind: RecordingState["kind"], message = "", seconds = this.state.seconds, submitted = false) {
    this.state = {kind, message, seconds, submitted}; this.update(this.state);
  }
  private release() {
    clearInterval(this.timer); clearTimeout(this.limit);
    this.stream?.getTracks().forEach(track => track.stop()); this.stream = undefined;
  }
  cancel(notify = true) {
    this.generation++; this.request?.abort(); this.request = undefined;
    if (this.audio) { this.audio.finalized = true; this.audio.chunks.length = 0; }
    this.audio = undefined;
    const recorder = this.recorder; this.recorder = undefined;
    if (recorder && recorder.state !== "inactive") recorder.stop();
    this.release(); this.state = idleRecording;
    if (notify) this.update(this.state);
  }
  async start() {
    if (recordingBusy(this.state)) return;
    const generation = ++this.generation;
    this.set("permission", "Разрешите доступ к микрофону…", 0);
    try {
      if (!globalThis.isSecureContext || !navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
        this.set("error", "Запись требует HTTPS или localhost и браузер с поддержкой MediaRecorder."); return;
      }
      const mimeType = MIME_TYPES.find(type => MediaRecorder.isTypeSupported(type));
      if (!mimeType) { this.set("error", "Этот браузер не поддерживает нужный формат записи. Попробуйте другой браузер."); return; }
      const stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true}, video: false});
      if (generation !== this.generation) { stream.getTracks().forEach(track => track.stop()); return; }
      this.stream = stream;
      const recorder = new MediaRecorder(stream, {mimeType, audioBitsPerSecond: 64000});
      this.recorder = recorder;
      const audio = {chunks: [] as Blob[], bytes: 0, finalized: false, submit: false};
      this.audio = audio;
      recorder.ondataavailable = event => {
        if (generation !== this.generation || audio.finalized || !event.data.size) return;
        audio.bytes += event.data.size;
        if (audio.bytes > MAX_BYTES) { this.cancel(); this.set("error", "Запись слишком большая. Попробуйте короче."); return; }
        audio.chunks.push(event.data);
      };
      recorder.onerror = () => { if (generation === this.generation) { this.cancel(); this.set("error", "Запись прервалась. Попробуйте ещё раз."); } };
      recorder.onstop = () => {
        if (generation !== this.generation || audio.finalized) return;
        audio.finalized = true;
        this.set("processing", "Распознаю речь…");
        const blob = new Blob(audio.chunks, {type: recorder.mimeType || mimeType});
        audio.chunks.length = 0; this.audio = undefined;
        this.release(); this.recorder = undefined;
        void this.upload(blob, generation, audio.submit);
      };
      stream.getTracks().forEach(track => track.addEventListener("ended", () => {
        if (generation === this.generation && this.state.kind === "recording") this.stop();
      }, {once: true}));
      recorder.start(250);
      const start = Date.now();
      this.set("recording", "Идёт запись", 0);
      this.timer = setInterval(() => {
        if (generation === this.generation && this.state.kind === "recording") this.set("recording", "Идёт запись", Math.floor((Date.now() - start) / 1000));
      }, 250);
      // Leave a second for encoder padding and delayed mobile callbacks; server independently enforces 60s.
      this.limit = setTimeout(() => this.stop(), 59000);
    } catch (error) {
      if (generation !== this.generation) return;
      this.cancel(); this.set("error", microphoneError(error));
    }
  }
  stop(submit = false) {
    if (this.state.kind !== "recording") return;
    if (this.audio) this.audio.submit = submit;
    clearInterval(this.timer); clearTimeout(this.limit);
    this.set("processing", "Распознаю речь…");
    if (this.recorder?.state === "recording") this.recorder.stop();
    this.release();
  }
  private async upload(blob: Blob, generation: number, submit: boolean) {
    if (!blob.size) { this.set("error", "Запись пустая. Повторите команду."); return; }
    this.set("processing", "Распознаю речь…");
    const controller = new AbortController(); this.request = controller;
    const timeout = setTimeout(() => controller.abort(), 70000);
    try {
      const response = await checked(await apiFetch("/api/v1/voice/transcriptions", {
        method: "POST", headers: {"Content-Type": blob.type}, body: blob, signal: controller.signal,
      }), "Не удалось распознать речь.");
      const body = await response.json();
      if (generation !== this.generation) return;
      if (typeof body.text !== "string" || !body.text.trim()) throw new Error("Не удалось разобрать речь. Повторите команду.");
      this.set("done", "Речь распознана.", this.state.seconds, submit);
      this.transcript(body.text.trim(), submit);
    } catch (error) {
      if (generation !== this.generation) return;
      this.set("error", controller.signal.aborted ? "Распознавание не ответило вовремя. Попробуйте ещё раз."
        : error instanceof TypeError || error instanceof SyntaxError ? "Нет корректного ответа сервера. Можно продолжить текстом."
        : error instanceof Error ? error.message : "Ошибка распознавания. Можно продолжить текстом.");
    } finally { clearTimeout(timeout); if (this.request === controller) this.request = undefined; }
  }
}

/** One player for the entire chat: never overlap responses, keep one in-memory replay. */
export class VoicePlayer {
  state = idlePlayback;
  private generation = 0;
  private request?: AbortController;
  private audio?: HTMLAudioElement;
  private url?: string;
  private cachedId = "";
  constructor(private update: (state: PlaybackState) => void) {}
  private set(kind: PlaybackState["kind"], id: string, message = "") {
    this.state = {kind, id, message}; this.update(this.state);
  }
  stop(notify = true) {
    this.generation++; this.request?.abort(); this.request = undefined;
    if (this.audio) { this.audio.pause(); this.audio.currentTime = 0; }
    this.state = idlePlayback; if (notify) this.update(this.state);
  }
  dispose() {
    this.stop(false); if (this.url) URL.revokeObjectURL(this.url);
    this.url = undefined; this.audio = undefined; this.cachedId = "";
  }
  async play(id: string, text: string) {
    if (this.state.id === id && ["loading", "playing"].includes(this.state.kind)) return;
    this.stop(); const generation = this.generation;
    if (!text) { this.set("error", id, "Ответ слишком длинный для озвучивания. Прочитайте его в чате."); return; }
    this.set("loading", id, "Готовлю голосовой ответ…");
    const controller = new AbortController(); this.request = controller;
    const timeout = setTimeout(() => controller.abort(), 70000);
    try {
      if (this.cachedId !== id || !this.audio) {
        if (this.url) URL.revokeObjectURL(this.url);
        this.audio = undefined; this.url = undefined; this.cachedId = "";
        const response = await checked(await apiFetch("/api/v1/voice/speech", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({text}), signal: controller.signal,
        }), "Озвучивание недоступно. Текст ответа доступен.");
        const blob = await response.blob();
        if (generation !== this.generation) return;
        if (!blob.size) throw new Error("Получен пустой голосовой ответ.");
        this.url = URL.createObjectURL(blob); this.audio = new Audio(this.url); this.cachedId = id;
      }
      const audio = this.audio;
      audio.onended = () => { if (generation === this.generation) this.set("idle", id); };
      audio.onerror = () => { if (generation === this.generation) { this.stop(); this.set("error", id, "Не удалось воспроизвести аудио. Текст ответа доступен."); } };
      await audio.play();
      if (generation !== this.generation) return;
      this.set("playing", id, "Воспроизводится ответ");
    } catch (error) {
      if (generation !== this.generation) return;
      if (error instanceof Error && error.name === "NotAllowedError") {
        this.set("blocked", id, "Браузер остановил автозапуск. Нажмите «Воспроизвести ответ».");
      } else this.set("error", id, controller.signal.aborted ? "Озвучивание не ответило вовремя. Текст ответа доступен."
        : error instanceof TypeError || error instanceof SyntaxError ? "Нет корректного ответа сервера озвучивания. Текст ответа доступен."
        : error instanceof Error ? error.message : "Ошибка воспроизведения.");
    } finally { clearTimeout(timeout); if (this.request === controller) this.request = undefined; }
  }
}
