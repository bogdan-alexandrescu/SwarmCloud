// VOICE DETECTION IN THE BROWSER (docs/design/voice-it-own-speech.md §3.3
// step 1; §14 row U1): only speech leaves the browser.
//
//   * PCM is 16 kHz, mono, 16-bit little-endian, in 20 ms frames of 640 bytes;
//   * the mic's rate is brought down to 16 kHz without a seam between blocks;
//   * silence sends nothing; speech starts an utterance with 300 ms of
//     pre-roll and ends after the hangover, a short dip inside it does not;
//   * muted, nothing is sent, and muting mid-utterance ends it at once;
//   * push-to-talk sends while held, whatever the model says, and ends on
//     release;
//   * Silero is fed its 64-sample context, its state carried, at 16 kHz;
//   * when the model cannot load, loudness gates instead, end to end.
//
// MUTATIONS: send frames while silent; drop the pre-roll; end on the first
// quiet window; send while muted; gate push-to-talk on the model; feed Silero
// the window without its context; reset the state each window; send
// everything when the model fails to load.

import { describe, expect, it, vi } from 'vitest'

import { MIC_START, type MicState } from '../voice/mic'
import {
  CONTEXT_SAMPLES,
  energyModel,
  FRAME_BYTES,
  FRAME_SAMPLES,
  HANGOVER_MS,
  pcm16,
  Resampler,
  SileroModel,
  SpeechGate,
  startVoiceDetection,
  VAD_RATE,
  VoiceDetector,
  WINDOW_SAMPLES,
  type OrtLike,
  type SpeechModel,
} from '../voice/vad'

const WINDOW_MS = (WINDOW_SAMPLES / VAD_RATE) * 1000

function recorder() {
  const events: string[] = []
  const frames: ArrayBuffer[] = []
  return {
    events,
    frames,
    h: {
      onStart: () => events.push('start'),
      onFrame: (f: ArrayBuffer) => {
        frames.push(f)
        events.push('frame')
      },
      onEnd: () => events.push('end'),
    },
  }
}

/** A window whose every sample is `v`. */
const win = (v: number) => new Float32Array(WINDOW_SAMPLES).fill(v)
const samplesOf = (f: ArrayBuffer) => Array.from(new Int16Array(f))

describe('PCM framing', () => {
  it('is 16-bit little-endian, clipped to the range', () => {
    const out = pcm16(Float32Array.from([0, 1, -1, 0.5, 2]))
    expect(out.byteLength).toBe(10)
    const v = new DataView(out)
    expect([0, 2, 4, 6, 8].map((o) => v.getInt16(o, true))).toEqual([0, 32767, -32768, 16384, 32767])
  })

  it('a frame is 20 ms at 16 kHz: 320 samples, 640 bytes', () => {
    expect(FRAME_SAMPLES).toBe(320)
    expect(FRAME_BYTES).toBe(640)
  })
})

describe('the resampler', () => {
  it('takes 48 kHz to 16 kHz, a third of the samples, the level kept', () => {
    const r = new Resampler(48_000)
    const out = r.push(new Float32Array(4_800).fill(0.25))
    expect(out.length).toBe(1_600)
    expect(out.every((x) => Math.abs(x - 0.25) < 1e-6)).toBe(true)
  })

  it('joins blocks of any length without a seam, at 44.1 kHz too', () => {
    const whole = new Resampler(44_100).push(new Float32Array(44_100).fill(0.1)).length
    const r = new Resampler(44_100)
    let n = 0
    for (const size of [128, 1000, 333, 42_639]) n += r.push(new Float32Array(size).fill(0.1)).length
    expect(n).toBe(whole)
    expect(whole).toBe(16_000)
  })
})

describe('the speech gate', () => {
  it('sends nothing while it is silent', () => {
    const r = recorder()
    const g = new SpeechGate(r.h)
    for (let i = 0; i < 50; i++) g.window(win(0.01), 0.05)
    expect(r.events).toEqual([])
  })

  it('starts on speech with 300 ms of pre-roll, the most recent, and frames of 640 bytes', () => {
    const r = recorder()
    const g = new SpeechGate(r.h)
    // Ten quiet windows, each marked by its index, so the pre-roll can be read back.
    for (let i = 1; i <= 10; i++) g.window(win(i / 100), 0.05)
    g.window(win(0.5), 0.9)
    expect(r.events[0]).toBe('start')
    // 300 ms (4,800 samples) of pre-roll plus the 512 of the window: 16 whole frames, 192 samples pending.
    expect(r.frames).toHaveLength(16)
    expect(r.frames.every((f) => f.byteLength === FRAME_BYTES)).toBe(true)
    const first = samplesOf(r.frames[0] as ArrayBuffer)
    // 4,800 samples back from the speech is inside quiet window 1 + (5,120 - 4,800) / 512: window 1, sample 320 on.
    expect(first[0]).toBe(Math.round(0.01 * 0x7fff))
    const all = r.frames.flatMap(samplesOf)
    expect(all.slice(-192)).toEqual(new Array(192).fill(Math.round(0.5 * 0x7fff)))
    expect(all).not.toContain(Math.round(0.11 * 0x7fff))
  })

  it('ends after the hangover of quiet, not on a short dip, and pads the last frame', () => {
    const r = recorder()
    const g = new SpeechGate(r.h)
    g.window(win(0.5), 0.9)
    // A dip between the thresholds counts for nothing.
    for (let i = 0; i < 40; i++) g.window(win(0.4), 0.4)
    expect(r.events).not.toContain('end')
    const quiet = Math.ceil(HANGOVER_MS / WINDOW_MS)
    for (let i = 0; i < quiet - 1; i++) g.window(win(0), 0.1)
    expect(r.events).not.toContain('end')
    g.window(win(0), 0.1)
    expect(r.events.at(-1)).toBe('end')
    expect(r.frames.every((f) => f.byteLength === FRAME_BYTES)).toBe(true)
    expect(g.inUtterance).toBe(false)
  })

  it('sends nothing while muted, and muting mid-utterance ends it at once', () => {
    const r = recorder()
    const muted: MicState = { ...MIC_START, muted: true }
    const g = new SpeechGate(r.h, muted)
    for (let i = 0; i < 10; i++) g.window(win(0.5), 0.95)
    expect(r.events).toEqual([])
    g.setMic(MIC_START)
    g.window(win(0.5), 0.95)
    expect(r.events[0]).toBe('start')
    // Nothing said before the unmute rides along as pre-roll.
    expect(r.frames).toHaveLength(1)
    g.setMic(muted)
    expect(r.events.at(-1)).toBe('end')
    const sent = r.frames.length
    g.window(win(0.5), 0.95)
    expect(r.frames).toHaveLength(sent)
  })

  it('on push-to-talk, sends while held whatever the model says, and ends on release', () => {
    const r = recorder()
    const ptt: MicState = { muted: false, mode: 'ptt', held: false }
    const g = new SpeechGate(r.h, ptt)
    g.window(win(0.5), 0.99)
    expect(r.events).toEqual([])
    g.setMic({ ...ptt, held: true })
    g.window(win(0.001), 0.0)
    expect(r.events[0]).toBe('start')
    for (let i = 0; i < 40; i++) g.window(win(0.001), 0.0)
    expect(r.events).not.toContain('end')
    g.setMic(ptt)
    expect(r.events.at(-1)).toBe('end')
  })
})

/** onnxruntime-web's surface, recorded. */
function fakeOrt(probs: number[]) {
  const feeds: Record<string, { type: string; data: Float32Array | BigInt64Array; dims: readonly number[] }>[] = []
  class Tensor {
    constructor(
      readonly type: string,
      readonly data: Float32Array | BigInt64Array,
      readonly dims: readonly number[],
    ) {}
  }
  let n = 0
  const ort: OrtLike = {
    Tensor: Tensor as unknown as OrtLike['Tensor'],
    InferenceSession: {
      create: async () => ({
        run: async (f: Record<string, unknown>) => {
          feeds.push(f as (typeof feeds)[number])
          n += 1
          return { output: { data: Float32Array.from([probs[n - 1] ?? 0]) }, stateN: new Tensor('float32', new Float32Array(256).fill(n), [2, 1, 128]) }
        },
      }),
    },
  }
  return { ort, feeds }
}

describe('Silero', () => {
  it('reads each window with the last 64 samples of the one before, its state carried, at 16 kHz', async () => {
    const { ort, feeds } = fakeOrt([0.2, 0.8])
    const m = await SileroModel.create(ort, new Uint8Array([1]))
    const a = Float32Array.from({ length: WINDOW_SAMPLES }, (_, i) => i / WINDOW_SAMPLES)
    expect(await m.probability(a)).toBeCloseTo(0.2)
    expect(await m.probability(win(0.3))).toBeCloseTo(0.8)
    const [one, two] = feeds
    expect(one?.input.dims).toEqual([1, CONTEXT_SAMPLES + WINDOW_SAMPLES])
    expect(Array.from(one?.input.data as Float32Array).slice(0, CONTEXT_SAMPLES)).toEqual(new Array(CONTEXT_SAMPLES).fill(0))
    expect(one?.state.dims).toEqual([2, 1, 128])
    expect(Array.from(one?.state.data as Float32Array).every((x) => x === 0)).toBe(true)
    expect(one?.sr.type).toBe('int64')
    expect(Array.from(one?.sr.data as BigInt64Array)).toEqual([16_000n])
    // The second window: the first's tail as context, the first's state out as state in.
    expect(Array.from((two?.input.data as Float32Array).slice(0, CONTEXT_SAMPLES))).toEqual(Array.from(a.slice(-CONTEXT_SAMPLES)))
    expect(Array.from(two?.state.data as Float32Array).every((x) => x === 1)).toBe(true)
    m.reset()
    await m.probability(win(0))
    expect(Array.from(feeds[2]?.state.data as Float32Array).every((x) => x === 0)).toBe(true)
  })
})

describe('the detector', () => {
  it('scores 16 kHz windows in order and sends only the speech', async () => {
    const r = recorder()
    let i = 0
    // Windows 3 to 5 are speech.
    const model: SpeechModel = { name: 'silero', probability: () => ((i += 1), i >= 3 && i <= 5 ? 0.9 : 0.05), reset: () => undefined }
    const d = new VoiceDetector(model, r.h, 48_000)
    for (let b = 0; b < 30; b++) await d.push(new Float32Array(1_024).fill(0.2))
    // 30 * 1024 / 3 = 10,240 samples at 16 kHz: twenty windows scored.
    expect(i).toBe(20)
    expect(r.events.filter((e) => e === 'start')).toHaveLength(1)
    expect(r.events).toContain('end')
    d.close()
    await d.push(new Float32Array(1_024))
    expect(i).toBe(20)
  })

  it('loudness: a quiet room is 0, speech near the mic is 1', () => {
    const e = energyModel()
    expect(e.probability(win(0.001))).toBe(0)
    expect(e.probability(win(0.2))).toBe(1)
  })
})

describe('startVoiceDetection', () => {
  it('falls back to loudness when the model cannot load, and still gates what it sends', async () => {
    let node: { onaudioprocess: ((e: unknown) => void) | null } | null = null
    class Ctx {
      sampleRate = 16_000
      state = 'running'
      destination = {}
      close = vi.fn(async () => undefined)
      createMediaStreamSource = () => ({ connect: vi.fn(), disconnect: vi.fn() })
      createGain = () => ({ gain: { value: 1 }, connect: vi.fn() })
      createScriptProcessor = () => {
        node = { onaudioprocess: null, connect: vi.fn(), disconnect: vi.fn() } as never
        return node
      }
    }
    vi.stubGlobal('AudioContext', Ctx)
    const r = recorder()
    const det = await startVoiceDetection({} as MediaStream, r.h, MIC_START, async () => {
      throw new Error('no wasm here')
    })
    expect(det.model).toBe('energy')
    const feed = (v: number) =>
      (node as unknown as { onaudioprocess: (e: unknown) => void }).onaudioprocess({ inputBuffer: { getChannelData: () => new Float32Array(1_024).fill(v) } })
    for (let k = 0; k < 4; k++) feed(0.0005)
    await Promise.resolve()
    await new Promise((res) => setTimeout(res, 0))
    expect(r.events).toEqual([])
    for (let k = 0; k < 2; k++) feed(0.3)
    await new Promise((res) => setTimeout(res, 0))
    expect(r.events[0]).toBe('start')
    det.setMic({ ...MIC_START, muted: true })
    expect(r.events.at(-1)).toBe('end')
    det.close()
  })
})
