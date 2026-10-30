import { Icon } from "./icons";
import type { VoiceControls } from "./use-voice";

export function VoiceButton({voice, disabled = false}: {voice?: VoiceControls; disabled?: boolean}) {
  const kind = voice?.recording.kind || "idle";
  const recording = kind === "recording";
  const waiting = kind === "permission" || kind === "processing";
  return <div className={`voice-control ${recording ? "voice-recording" : ""}`}>
    <button type="button" className="mic-button" disabled={disabled || !voice}
      aria-label={recording ? "Отменить запись" : waiting ? "Отменить запись или распознавание" : "Начать голосовой ввод"}
      aria-pressed={recording} onClick={recording || waiting ? voice?.cancel : voice?.start}>
      {recording || waiting ? <span className="voice-stop-icon" /> : <Icon name="mic" width="24" height="24" />}
    </button>
    <span className="voice-caption">{recording ? `0:${String(voice?.recording.seconds || 0).padStart(2, "0")}` : waiting ? "Отмена" : "Голос"}</span>
  </div>;
}
