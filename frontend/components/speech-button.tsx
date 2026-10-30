import type { PlaybackState } from "@/lib/voice";

export function SpeechButton({id, state, disabled, onPlay, onStop}: {
  id: string; state: PlaybackState; disabled: boolean; onPlay: () => void; onStop: () => void;
}) {
  const current = state.id === id;
  const active = current && (state.kind === "playing" || state.kind === "loading");
  return <div className="speech-control">
    <button type="button" className="speech-button" disabled={disabled && !active}
      onClick={active ? onStop : onPlay} aria-label={active ? "Остановить озвучивание" : "Воспроизвести ответ"}>
      <span aria-hidden="true">{active ? "■" : "▷"}</span>
      {active ? state.kind === "loading" ? "Отменить озвучивание" : "Остановить" : "Озвучить"}
    </button>
    {current && state.message && <span role={state.kind === "error" ? "alert" : "status"}>{state.message}</span>}
  </div>;
}
