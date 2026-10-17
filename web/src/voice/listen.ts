import { api, type VoiceStatus } from "../api";
import { say, shortError } from "../ui/actions";
import { micLevel, useVoice } from "./voiceStore";
import { toWav } from "./wav";

const MIN_RECORDING_MS = 450;
const MAX_RECORDING_MS = 12_000;

let status: VoiceStatus | null = null;
let statusLoad: Promise<VoiceStatus | null> | null = null;

export function voiceStatus(): VoiceStatus | null {
  return status;
}

/** Asks the backend whether it can transcribe audio. A failed request is retried the next time someone talks. */
export function loadVoiceStatus(): Promise<VoiceStatus | null> {
  statusLoad ??= api
    .voiceStatus()
    .then((value) => (status = value))
    .catch(() => {
      statusLoad = null;
      return null;
    });
  return statusLoad;
}

interface RecognitionResultLike {
  isFinal: boolean;
  0: { transcript: string };
}

interface RecognitionLike {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onresult: ((event: { results: ArrayLike<RecognitionResultLike> }) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}

function recognitionClass(): (new () => RecognitionLike) | null {
  const w = window as unknown as {
    SpeechRecognition?: new () => RecognitionLike;
    webkitSpeechRecognition?: new () => RecognitionLike;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

function canRecord(): boolean {
  return typeof MediaRecorder !== "undefined" && !!navigator.mediaDevices?.getUserMedia;
}

/** Ends a push-to-talk turn and yields what was said, or an empty string when there was nothing to take. */
interface Turn {
  stop(): Promise<string>;
  abort(): void;
}

function recordedType(): string | undefined {
  for (const type of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"]) {
    if (MediaRecorder.isTypeSupported(type)) return type;
  }
  return undefined;
}

function containerOf(type: string): string {
  if (type.includes("webm")) return "webm";
  if (type.includes("mp4") || type.includes("aac")) return "mp4";
  if (type.includes("ogg")) return "ogg";
  if (type.includes("wav")) return "wav";
  return "webm";
}

async function transcribe(blob: Blob, accepted: readonly string[]): Promise<string> {
  const upload = accepted.includes(containerOf(blob.type)) ? blob : await toWav(blob);
  const { transcript } = await api.voice(upload);
  return typeof transcript === "string" ? transcript.trim() : "";
}

async function openRecorder(accepted: readonly string[]): Promise<Turn> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
  });
  const type = recordedType();
  const recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
  const chunks: Blob[] = [];
  recorder.ondataavailable = (event) => {
    if (event.data.size > 0) chunks.push(event.data);
  };

  const context = new AudioContext();
  const analyser = context.createAnalyser();
  analyser.fftSize = 512;
  context.createMediaStreamSource(stream).connect(analyser);
  const samples = new Uint8Array(analyser.fftSize);
  let meter = 0;
  const measure = () => {
    analyser.getByteTimeDomainData(samples);
    let sum = 0;
    for (const value of samples) sum += ((value - 128) / 128) ** 2;
    micLevel.value = Math.min(1, Math.sqrt(sum / samples.length) * 4);
    meter = requestAnimationFrame(measure);
  };
  measure();

  const startedAt = performance.now();
  const closed = new Promise<void>((resolve) => {
    recorder.onstop = () => resolve();
  });
  recorder.start();

  const release = () => {
    cancelAnimationFrame(meter);
    micLevel.value = 0;
    for (const track of stream.getTracks()) track.stop();
    void context.close();
  };
  const cap = window.setTimeout(() => {
    if (recorder.state === "recording") recorder.stop();
  }, MAX_RECORDING_MS);

  return {
    async stop() {
      window.clearTimeout(cap);
      if (recorder.state === "recording") recorder.stop();
      await closed;
      release();
      if (performance.now() - startedAt < MIN_RECORDING_MS || chunks.length === 0) return "";
      useVoice.setState({ listen: "transcribing" });
      return transcribe(new Blob(chunks, { type: recorder.mimeType || type || "audio/webm" }), accepted);
    },
    abort() {
      window.clearTimeout(cap);
      if (recorder.state === "recording") recorder.stop();
      release();
    },
  };
}

function openRecognition(Recognition: new () => RecognitionLike): Turn {
  const recognition = new Recognition();
  recognition.lang = "en-US";
  recognition.interimResults = true;
  recognition.continuous = false;

  let heard = "";
  let failure = "";
  const ended = new Promise<void>((resolve) => {
    recognition.onend = () => resolve();
  });
  recognition.onresult = (event) => {
    heard = Array.from(event.results)
      .map((result) => result[0].transcript)
      .join(" ")
      .trim();
  };
  recognition.onerror = (event) => {
    if (event.error !== "no-speech" && event.error !== "aborted") failure = event.error;
  };
  recognition.start();

  return {
    async stop() {
      recognition.stop();
      await Promise.race([ended, new Promise<void>((resolve) => window.setTimeout(resolve, 2500))]);
      if (failure && !heard) {
        throw new Error(
          failure === "not-allowed" || failure === "service-not-allowed"
            ? "microphone access was refused"
            : `the browser could not recognise speech (${failure})`,
        );
      }
      return heard;
    },
    abort() {
      recognition.abort();
    },
  };
}

let turn: Turn | null = null;
let opening: Promise<void> | null = null;
let stopAsked = false;
let deliver: (transcript: string) => void = () => {};

/** Where a finished transcript goes. Set once by the voice flow. */
export function onTranscript(handler: (transcript: string) => void): void {
  deliver = handler;
}

async function chooseTurn(): Promise<Turn | null> {
  const current = (await loadVoiceStatus()) ?? status;
  if (current?.available && canRecord()) return openRecorder(current.formats?.length ? current.formats : ["wav"]);
  const Recognition = recognitionClass();
  if (Recognition) return openRecognition(Recognition);
  return null;
}

export async function startListening(): Promise<void> {
  if (turn || opening) return;
  stopAsked = false;
  useVoice.setState({ listen: "listening" });
  opening = (async () => {
    try {
      const next = await chooseTurn();
      if (!next) {
        say("Voice input is not available in this browser. Type a command instead.");
        useVoice.setState({ listen: "idle" });
        return;
      }
      turn = next;
      if (stopAsked) await stopListening();
    } catch (error) {
      useVoice.setState({ listen: "idle" });
      const text = error instanceof DOMException && error.name === "NotAllowedError"
        ? "Microphone access was refused."
        : `Voice input failed: ${shortError(error)}`;
      say(text);
    } finally {
      opening = null;
    }
  })();
  await opening;
}

export async function stopListening(): Promise<void> {
  stopAsked = true;
  const current = turn;
  if (!current) return;
  turn = null;
  try {
    const transcript = await current.stop();
    useVoice.setState({ listen: "idle" });
    if (transcript) deliver(transcript);
    else say("I did not catch that. Hold the key while you speak.", "low");
  } catch (error) {
    useVoice.setState({ listen: "idle" });
    say(`Voice input failed: ${shortError(error)}`);
  }
}

export function abortListening(): void {
  turn?.abort();
  turn = null;
  stopAsked = true;
  useVoice.setState({ listen: "idle" });
}
