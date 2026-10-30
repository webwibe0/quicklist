import { useLayoutEffect, type FormEvent, type KeyboardEvent, type RefObject, type PointerEvent } from "react";
import { Icon } from "./icons";
import { VoiceButton } from "./voice-button";
import type { VoiceControls } from "./use-voice";
import { recordingBusy } from "@/lib/voice";
import { AudioWaveform } from "./audio-waveform";
import { AssistantState, assistantState, type AssistantStateValue } from "./assistant-state";

export function submitOnEnter(event: KeyboardEvent<HTMLTextAreaElement>) {
  if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  }
}

export function Composer({value, onChange, onSubmit, inputRef, busy, ready, state, voice}: {
  value: string; onChange: (value: string) => void; onSubmit: (event: FormEvent) => void;
  inputRef: RefObject<HTMLTextAreaElement | null>; busy: boolean; ready: boolean;
  state?: AssistantStateValue;
  voice?: VoiceControls;
}) {
  const locked = !!voice && recordingBusy(voice.recording);
  const recording = voice?.recording.kind === "recording";
  function reflect(event: PointerEvent<HTMLFormElement>) {
    if (event.pointerType !== "mouse" || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const rect = event.currentTarget.getBoundingClientRect();
    event.currentTarget.style.setProperty("--light-x", `${(event.clientX - rect.left) / rect.width * 100}%`);
    event.currentTarget.style.setProperty("--light-y", `${(event.clientY - rect.top) / rect.height * 100}%`);
  }
  useLayoutEffect(() => {
    const field = inputRef.current;
    if (field) { field.style.height = "auto"; field.style.height = `${Math.min(148, field.scrollHeight)}px`; }
  }, [value, inputRef]);
  return <div className="composer-wrap">
    <AssistantState state={state || assistantState([], busy, "", ready)} />
    <form className={`composer${recording ? " is-recording" : ""}`} onSubmit={event => {
      if (recording) { event.preventDefault(); voice?.submitRecording(); }
      else onSubmit(event);
    }} aria-label="Отправить сообщение ассистенту"
      onPointerMove={reflect} onPointerLeave={event => {event.currentTarget.style.removeProperty("--light-x"); event.currentTarget.style.removeProperty("--light-y");}}>
      <div className="composer-input-row">
      <div className="composer-field">
      <label htmlFor="message" className="sr-only">Сообщение ассистенту</label>
      <textarea id="message" ref={inputRef} value={value} rows={1} maxLength={4000}
        onChange={event => onChange(event.target.value)} onKeyDown={submitOnEnter}
        disabled={busy || !ready || locked} placeholder="Напишите, что нужно сделать…" />
      {recording && voice?.getStream && <AudioWaveform getStream={voice.getStream} />}
      </div>
      <div className="composer-actions">
      <VoiceButton voice={voice} disabled={busy || !ready} />
      <button className="button button-primary send-button" type="submit" disabled={busy || !ready || (!recording && (locked || !value.trim()))} aria-label={recording ? "Отправить голос" : "Отправить сообщение"}>
        <span className="sr-only">{busy ? "Обрабатываю…" : "Отправить"}</span><Icon name="send" width="29" height="29" />
      </button>
      </div>
      </div>
      <div className="composer-toolbar">
        <div className="composer-left">
          {voice && <label className="auto-speak-control">
                <input
                  type="checkbox" role="switch"
                  checked={voice.autoSpeak}
                  onChange={event => {
                    voice.setAutoSpeak(event.target.checked);
                    if (!event.target.checked) voice.stopPlayback();
                  }}
                />
                <span>Озвучивать ответы автоматически</span>
          </label>}
        </div>
      </div>
    </form>
    {voice && <div className="voice-options">
      {voice.recording.message && <div className={`voice-feedback voice-${voice.recording.kind}`}
        role={voice.recording.kind === "error" ? "alert" : "status"}>
        <span>{voice.recording.message}{voice.recording.kind === "done" && !voice.autoSend && !voice.recording.submitted ? " Проверьте текст и нажмите «Отправить»." : ""}</span>
      </div>}
    </div>}
    <p className="composer-note">Если не хватит деталей, ассистент уточнит. Вы всегда можете ответить текстом.</p>
  </div>;
}
