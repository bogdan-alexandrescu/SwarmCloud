/**
 * VOICE IT'S EARS IN THE BROWSER: voice detection that gates what the Talk
 * page sends to our own speech engine (docs/design/voice-it-own-speech.md
 * §3.3 step 1).
 *
 * Silero VAD runs here through onnxruntime-web, the model file bundled with
 * the console at `SILERO_MODEL_URL` (no CDN). Only speech leaves the browser:
 * the frames inside an utterance, plus 300 ms of pre-roll so the first
 * syllable is not clipped, as 16 kHz, mono, 16-bit little-endian PCM in
 * 20 ms binary frames (640 bytes). While nobody speaks, nothing is sent.
 *
 * THE PIPELINE. An AudioWorklet taps the mic's stream and posts its samples
 * to the page; `Resampler` brings them to 16 kHz; `VoiceDetector` cuts them
 * into Silero's 512-sample windows and asks the model each window's speech
 * probability; `SpeechGate` turns probabilities into utterances: it starts at
 * `SPEECH_ON`, ends after `HANGOVER_MS` below `SPEECH_OFF`, and emits the
 * frames between.
 *
 * MUTE AND PUSH-TO-TALK (mic.ts's state). Muted, nothing is sent and an
 * utterance in flight ends. Push-to-talk sends while it is held, whatever the
 * model says (holding is the person saying "I am speaking"), and ends the
 * utterance on release, so what was said is still heard. Open mic is the
 * model's call.
 *
 * WHEN THE MODEL CANNOT LOAD (a browser without WebAssembly, the file
 * missing), the detector falls back to the signal's loudness (`energyModel`),
 * so the page still gates what it sends rather than sending everything.
 */

import { micEnabled, type MicState } from './mic'

/** The rate the speech server takes, and Silero's. */
export const VAD_RATE = 16_000
/** 20 ms at 16 kHz: one frame on the socket. */
export const FRAME_SAMPLES = 320
/** One frame as 16-bit PCM. */
export const FRAME_BYTES = FRAME_SAMPLES * 2
/** Silero v5's window at 16 kHz (32 ms). */
export const WINDOW_SAMPLES = 512
/** Silero v5 reads each window with the last 64 samples of the one before. */
export const CONTEXT_SAMPLES = 64
/** Speech kept from before the start, so the first syllable is sent too. */
export const PREROLL_MS = 300
/** The probability that starts an utterance (Silero's own default). */
export const SPEECH_ON = 0.5
/** Below this, a window counts toward the end of the utterance. */
export const SPEECH_OFF = 0.35
/** Silence that ends an utterance: a short pause inside a sentence does not. */
export const HANGOVER_MS = 600
/** The model file, under the console's public directory. */
export const SILERO_MODEL_URL = '/vad/silero_vad.onnx'

const WINDOW_MS = (WINDOW_SAMPLES / VAD_RATE) * 1000
const PREROLL_SAMPLES = (PREROLL_MS / 1000) * VAD_RATE

/** Float samples in -1..1 as 16-bit little-endian PCM. */
export function pcm16(samples: Float32Array): ArrayBuffer {
  const out = new ArrayBuffer(samples.length * 2)
  const view = new DataView(out)
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i] ?? 0))
    view.setInt16(i * 2, s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7fff), true)
  }
  return out
}

/**
 * Down to 16 kHz from the context's rate (48 kHz, 44.1 kHz, ...): each output
 * sample is the mean of the input samples it covers, which is also the
 * low-pass that keeps speech from aliasing. The input and output counts carry
 * from one block to the next, so blocks of any length join without a seam.
 */
export class Resampler {
  private seen = 0
  private made = 0
  private sum = 0
  private count = 0

  constructor(
    readonly from: number,
    readonly to: number = VAD_RATE,
  ) {
    if (!(from > 0) || !(to > 0)) throw new RangeError('A sample rate must be positive.')
  }

  push(input: Float32Array): Float32Array {
    if (this.from === this.to) return input.slice()
    const out: number[] = []
    for (let i = 0; i < input.length; i++) {
      this.sum += input[i] ?? 0
      this.count += 1
      this.seen += 1
      // Whole-number counts, not a fractional step: a step like 2.75625 drifts a sample a second.
      if (this.seen * this.to >= (this.made + 1) * this.from) {
        out.push(this.sum / this.count)
        this.made += 1
        this.sum = 0
        this.count = 0
      }
    }
    return Float32Array.from(out)
  }
}

/** A window's speech probability, 0..1. */
export interface SpeechModel {
  readonly name: 'silero' | 'energy'
  probability(window: Float32Array): Promise<number> | number
  reset(): void
}

/** The parts of onnxruntime-web the model uses: a test hands in a fake. */
export interface OrtLike {
  Tensor: new (type: 'float32' | 'int64', data: Float32Array | BigInt64Array, dims: readonly number[]) => unknown
  InferenceSession: { create(model: Uint8Array): Promise<OrtSession> }
}
export interface OrtSession {
  run(feeds: Record<string, unknown>): Promise<Record<string, unknown>>
}

/** Silero VAD v5: `input` [1, 64 + 512], `state` [2, 1, 128], `sr`; out `output` [1, 1] and `stateN`. */
export class SileroModel implements SpeechModel {
  readonly name = 'silero' as const
  private state: unknown
  private context = new Float32Array(CONTEXT_SAMPLES)
  private readonly sr: unknown

  private constructor(
    private readonly ort: OrtLike,
    private readonly session: OrtSession,
  ) {
    this.state = this.zeroState()
    this.sr = new ort.Tensor('int64', BigInt64Array.from([BigInt(VAD_RATE)]), [])
  }

  static async create(ort: OrtLike, model: Uint8Array): Promise<SileroModel> {
    return new SileroModel(ort, await ort.InferenceSession.create(model))
  }

  /** The bundled model through onnxruntime-web, loaded only when own speech starts. */
  static async load(url: string = SILERO_MODEL_URL): Promise<SileroModel> {
    const ort = await import('onnxruntime-web/wasm')
    // No cross-origin isolation on the console, so no worker threads: one is enough for 32 ms windows.
    ort.env.wasm.numThreads = 1
    const r = await fetch(url)
    if (!r.ok) throw new Error(`The voice detection model did not load (${r.status}).`)
    return SileroModel.create(ort as unknown as OrtLike, new Uint8Array(await r.arrayBuffer()))
  }

  private zeroState(): unknown {
    return new this.ort.Tensor('float32', new Float32Array(2 * 128), [2, 1, 128])
  }

  async probability(window: Float32Array): Promise<number> {
    const x = new Float32Array(CONTEXT_SAMPLES + window.length)
    x.set(this.context, 0)
    x.set(window, CONTEXT_SAMPLES)
    const out = await this.session.run({
      input: new this.ort.Tensor('float32', x, [1, x.length]),
      state: this.state,
      sr: this.sr,
    })
    this.state = out.stateN
    this.context = window.slice(window.length - CONTEXT_SAMPLES)
    const output = out.output as { data?: ArrayLike<number> } | undefined
    return Number(output?.data?.[0] ?? 0)
  }

  reset(): void {
    this.state = this.zeroState()
    this.context = new Float32Array(CONTEXT_SAMPLES)
  }
}

/** Loudness as a probability: RMS 0.01 (quiet room) is 0, 0.05 (speech near the mic) is 1. */
export function energyModel(): SpeechModel {
  return {
    name: 'energy',
    probability(window: Float32Array): number {
      let sq = 0
      for (let i = 0; i < window.length; i++) sq += (window[i] ?? 0) ** 2
      const rms = Math.sqrt(sq / Math.max(1, window.length))
      return Math.max(0, Math.min(1, (rms - 0.01) / 0.04))
    },
    reset() {},
  }
}

export interface GateHandlers {
  /** An utterance begins: its frames follow, pre-roll first. */
  onStart: () => void
  /** One 20 ms frame of 16-bit PCM (`FRAME_BYTES`). */
  onFrame: (frame: ArrayBuffer) => void
  /** The utterance is over: send what is left of it and say so. */
  onEnd: () => void
}

/**
 * Probabilities in, utterances out. Feed it every 16 kHz window with the
 * model's probability; it holds the pre-roll and emits whole frames. The
 * mic's state (mute, push-to-talk) decides whether anything may be sent.
 */
export class SpeechGate {
  private mic: MicState
  private speaking = false
  private quietMs = 0
  private preroll: Float32Array[] = []
  private prerollSamples = 0
  private pending: number[] = []

  constructor(
    private readonly h: GateHandlers,
    mic: MicState = { muted: false, mode: 'open', held: false },
  ) {
    this.mic = mic
  }

  get inUtterance(): boolean {
    return this.speaking
  }

  /** The mic changed: muting or letting go of push-to-talk ends an utterance in flight. */
  setMic(mic: MicState): void {
    this.mic = mic
    if (this.speaking && !micEnabled(mic)) this.end()
  }

  window(samples: Float32Array, probability: number): void {
    if (!micEnabled(this.mic)) {
      // A muted word is never sent, and is not kept as pre-roll either.
      if (this.speaking) this.end()
      this.preroll = []
      this.prerollSamples = 0
      return
    }
    const ptt = this.mic.mode === 'ptt'
    if (!this.speaking) {
      const begins = ptt ? this.mic.held : probability >= SPEECH_ON
      if (!begins) {
        this.keep(samples)
        return
      }
      this.speaking = true
      this.quietMs = 0
      this.h.onStart()
      for (const block of this.preroll) this.emit(block)
      this.preroll = []
      this.prerollSamples = 0
      this.emit(samples)
      return
    }
    this.emit(samples)
    if (ptt) return
    if (probability >= SPEECH_ON) this.quietMs = 0
    else if (probability < SPEECH_OFF) this.quietMs += WINDOW_MS
    if (this.quietMs >= HANGOVER_MS) this.end()
  }

  /** End the utterance now (a hang-up, a mute): the partial frame is padded and sent. */
  end(): void {
    if (!this.speaking) return
    if (this.pending.length > 0) {
      const last = new Float32Array(FRAME_SAMPLES)
      last.set(this.pending)
      this.pending = []
      this.h.onFrame(pcm16(last))
    }
    this.speaking = false
    this.quietMs = 0
    this.h.onEnd()
  }

  private keep(samples: Float32Array): void {
    this.preroll.push(samples)
    this.prerollSamples += samples.length
    while (this.preroll.length > 0 && this.prerollSamples - (this.preroll[0]?.length ?? 0) >= PREROLL_SAMPLES) {
      this.prerollSamples -= this.preroll.shift()?.length ?? 0
    }
    // Trim the oldest block to exactly the pre-roll, so it is 300 ms and not up to a window more.
    const over = this.prerollSamples - PREROLL_SAMPLES
    if (over > 0 && this.preroll[0] !== undefined) {
      this.preroll[0] = this.preroll[0].slice(over)
      this.prerollSamples -= over
    }
  }

  private emit(samples: Float32Array): void {
    for (let i = 0; i < samples.length; i++) {
      this.pending.push(samples[i] ?? 0)
      if (this.pending.length === FRAME_SAMPLES) {
        this.h.onFrame(pcm16(Float32Array.from(this.pending)))
        this.pending = []
      }
    }
  }
}

/**
 * The detector: samples at the input rate in, gated frames out. Windows are
 * scored one after another, in order, however fast blocks arrive.
 */
export class VoiceDetector {
  private readonly resampler: Resampler
  private readonly gate: SpeechGate
  private buf: number[] = []
  private chain: Promise<void> = Promise.resolve()
  private closed = false

  constructor(
    readonly model: SpeechModel,
    handlers: GateHandlers,
    inputRate: number,
    mic?: MicState,
  ) {
    this.resampler = new Resampler(inputRate, VAD_RATE)
    this.gate = new SpeechGate(handlers, mic)
  }

  get inUtterance(): boolean {
    return this.gate.inUtterance
  }

  setMic(mic: MicState): void {
    this.gate.setMic(mic)
  }

  /** One block from the mic. Resolves once every window it completed is scored. */
  push(samples: Float32Array): Promise<void> {
    if (this.closed) return this.chain
    const at16 = this.resampler.push(samples)
    for (let i = 0; i < at16.length; i++) this.buf.push(at16[i] ?? 0)
    while (this.buf.length >= WINDOW_SAMPLES) {
      const w = Float32Array.from(this.buf.splice(0, WINDOW_SAMPLES))
      this.chain = this.chain.then(async () => {
        if (this.closed) return
        let p = 0
        try {
          p = await this.model.probability(w)
        } catch {
          // A window the model could not score counts as silence: nothing is sent for it.
          p = 0
        }
        if (!this.closed) this.gate.window(w, p)
      })
    }
    return this.chain
  }

  /** Stop: an utterance in flight ends (its frames sent), and nothing more is scored. */
  close(): void {
    if (this.closed) return
    this.gate.end()
    this.closed = true
    this.model.reset()
  }
}

/** The worklet: posts the mic's samples to the page in blocks of 1,024. */
const TAP_WORKLET = `
class SwarmVadTap extends AudioWorkletProcessor {
  constructor() { super(); this.buf = new Float32Array(1024); this.n = 0 }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0]
    if (ch) {
      for (let i = 0; i < ch.length; i++) {
        this.buf[this.n++] = ch[i]
        if (this.n === this.buf.length) { this.port.postMessage(this.buf.slice(0)); this.n = 0 }
      }
    }
    return true
  }
}
registerProcessor('swarm-vad-tap', SwarmVadTap)
`

export interface VoiceDetection {
  readonly model: SpeechModel['name']
  setMic(mic: MicState): void
  /** Ends an utterance in flight and releases the audio graph (not the mic: its owner stops it). */
  close(): void
}

type AudioCtor = new () => AudioContext

/**
 * Voice detection on the mic's stream: Silero when it loads, loudness when it
 * does not. Throws only when the browser has no Web Audio at all.
 */
export async function startVoiceDetection(
  stream: MediaStream,
  handlers: GateHandlers,
  mic: MicState,
  loadModel: () => Promise<SpeechModel> = () => SileroModel.load(),
): Promise<VoiceDetection> {
  const w = window as unknown as { AudioContext?: AudioCtor; webkitAudioContext?: AudioCtor }
  const Klass = w.AudioContext ?? w.webkitAudioContext
  if (Klass === undefined) throw new Error('This browser has no Web Audio, so it cannot detect speech.')
  let model: SpeechModel
  try {
    model = await loadModel()
  } catch {
    model = energyModel()
  }
  const ctx = new Klass()
  if (ctx.state === 'suspended') void ctx.resume().catch(() => undefined)
  const detector = new VoiceDetector(model, handlers, ctx.sampleRate, mic)
  const source = ctx.createMediaStreamSource(stream)
  // Silent, but connected: a node the graph does not pull from is not run.
  const sink = ctx.createGain()
  sink.gain.value = 0
  sink.connect(ctx.destination)
  let tap: AudioNode
  let url: string | null = null
  if (ctx.audioWorklet !== undefined && typeof AudioWorkletNode !== 'undefined') {
    url = URL.createObjectURL(new Blob([TAP_WORKLET], { type: 'application/javascript' }))
    await ctx.audioWorklet.addModule(url)
    const node = new AudioWorkletNode(ctx, 'swarm-vad-tap')
    node.port.onmessage = (e: MessageEvent<Float32Array>) => void detector.push(e.data)
    tap = node
  } else {
    // Older browsers: the deprecated processor does the same job on the main thread.
    const node = ctx.createScriptProcessor(1024, 1, 1)
    node.onaudioprocess = (e) => void detector.push(e.inputBuffer.getChannelData(0).slice(0))
    tap = node
  }
  source.connect(tap)
  tap.connect(sink)
  return {
    model: model.name,
    setMic: (m) => detector.setMic(m),
    close: () => {
      detector.close()
      try {
        source.disconnect()
        tap.disconnect()
      } catch {
        // A graph already torn down by the browser has nothing left to disconnect.
      }
      if (url !== null) URL.revokeObjectURL(url)
      void ctx.close().catch(() => undefined)
    },
  }
}
