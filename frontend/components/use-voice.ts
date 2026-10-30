"use client";
import { useEffect, useRef, useState } from "react";
import { idlePlayback, idleRecording, VoicePlayer, VoiceRecorder } from "@/lib/voice";

export function useVoice(onTranscript: (text: string, submit: boolean) => void) {
  const [recording, setRecording] = useState(idleRecording);
  const [playback, setPlayback] = useState(idlePlayback);
  const [autoSend, setAutoSend] = useState(false);
  const [autoSpeak, setAutoSpeak] = useState(false);
  const transcript = useRef(onTranscript); transcript.current = onTranscript;
  const recorder = useRef<VoiceRecorder | null>(null);
  const player = useRef<VoicePlayer | null>(null);
  useEffect(() => {
    recorder.current = new VoiceRecorder(setRecording, (text, submit) => transcript.current(text, submit));
    player.current = new VoicePlayer(setPlayback);
    const hidden = () => {
      if (document.hidden) { recorder.current?.cancel(); player.current?.stop(); }
    };
    document.addEventListener("visibilitychange", hidden);
    return () => {
      document.removeEventListener("visibilitychange", hidden);
      recorder.current?.cancel(false); player.current?.dispose();
    };
  }, []);
  return {recording, playback, autoSend, setAutoSend, autoSpeak, setAutoSpeak,
    getStream: () => recorder.current?.getStream(),
    start: () => { player.current?.stop(); void recorder.current?.start(); },
    stopRecording: () => recorder.current?.stop(), cancel: () => recorder.current?.cancel(),
    submitRecording: () => recorder.current?.stop(true),
    play: (id: string, text: string) => { void player.current?.play(id, text); },
    stopPlayback: () => player.current?.stop(),
    reset: () => { recorder.current?.cancel(); player.current?.dispose(); setPlayback(idlePlayback); },
  };
}
export type VoiceControls = ReturnType<typeof useVoice>;
