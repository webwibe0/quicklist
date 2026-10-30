import type { RefObject } from "react";
import type { ChatEntry, Event, EventAction } from "@/types/assistant";
import { Icon } from "./icons";
import { EmptyState } from "./empty-state";
import { EventCard } from "./event-card";
import { ConfirmationCard } from "./confirmation-card";
import { LoadingIndicator } from "./loading-indicator";
import { SpeechButton } from "./speech-button";
import { speechText, recordingBusy } from "@/lib/voice";
import type { VoiceControls } from "./use-voice";

export function ChatThread({entries, pendingText, busy, ready, error, endRef, onExample, onReply, onAction, voice}: {
  entries: ChatEntry[]; pendingText: string; busy: boolean; ready: boolean; error: string;
  endRef: RefObject<HTMLDivElement | null>; onExample: (text: string) => void;
  onReply: (text: string) => void; onAction: (action: EventAction, event: Event) => void;
  voice?: VoiceControls;
}) {
  const lastAssistant = entries.findLastIndex(entry => entry.role === "assistant");
  return <div className="chat-scroll"><div className="chat-content" aria-label="История диалога">
    {entries.length === 0 && !busy && <EmptyState onExample={onExample} disabled={!ready} />}
    {entries.map((entry, index) => entry.role === "user"
      ? <div className="user-message" key={index}><p>{entry.text}</p></div>
      : <div className="assistant-message" key={index}>
        <div className="assistant-avatar"><Icon name="sparkle" /></div>
        <div className="assistant-body"><p className="message-author">QuickList <span>Ассистент</span></p>
          {entry.requires_input && entry.missing_fields?.includes("confirmation")
            ? <ConfirmationCard text={entry.text} action={entry.action} active={index === lastAssistant}
                disabled={busy || !ready} onReply={onReply} />
            : <><p className="assistant-text">{entry.text}</p>
              {entry.requires_input && <span className="clarification-label">Нужно уточнить</span>}</>}
          {!!entry.events?.length && <div className="event-grid">{entry.events.map(event =>
            <EventCard key={event.id} event={event} onAction={onAction} disabled={busy || !ready} />)}</div>}
          {voice && <SpeechButton id={String(index)} state={voice.playback}
            disabled={busy || recordingBusy(voice.recording)} onStop={voice.stopPlayback}
            onPlay={() => voice.play(String(index), speechText(entry))} />}
        </div>
      </div>)}
    {busy && <><div className="user-message pending-message"><p>{pendingText}</p></div><LoadingIndicator /></>}
    {error && <div className="chat-error" role="alert"><Icon name="alert" /><p>{error}</p></div>}
    <div ref={endRef} />
  </div></div>;
}
