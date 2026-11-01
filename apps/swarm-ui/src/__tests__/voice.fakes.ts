// THE BROWSER'S MEDIA, FAKED for the Talk pane's suites (voice.talk and
// voice.mic). jsdom has no RTCPeerConnection, no getUserMedia and no
// AudioContext; these stand in for them with just the surface
// voice/rtc.ts and voice/Scope.tsx use, and record what was asked of them.

import { vi } from 'vitest'

export class FakeTrack {
  kind = 'audio'
  enabled = true
  readyState = 'live'
  stop = vi.fn(() => {
    this.readyState = 'ended'
  })
}

export class FakeStream {
  constructor(readonly tracks: FakeTrack[]) {}
  getTracks(): FakeTrack[] {
    return this.tracks
  }
  getAudioTracks(): FakeTrack[] {
    return this.tracks
  }
}

export class FakeChannel extends EventTarget {
  readyState = 'open'
  send = vi.fn()
  close = vi.fn()
  constructor(readonly label: string) {
    super()
  }
  /** One server event, as OpenAI sends it on the data channel. */
  deliver(event: Record<string, unknown>): void {
    this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(event) }))
  }
}

export class FakePeer extends EventTarget {
  static all: FakePeer[] = []
  static get last(): FakePeer {
    const p = FakePeer.all[FakePeer.all.length - 1]
    if (p === undefined) throw new Error('no RTCPeerConnection was made')
    return p
  }
  sent: { track: FakeTrack; stream: FakeStream }[] = []
  channels: FakeChannel[] = []
  local: RTCSessionDescriptionInit | null = null
  remote: RTCSessionDescriptionInit | null = null
  closed = false
  connectionState = 'new'
  constructor() {
    super()
    FakePeer.all.push(this)
  }
  addTrack(track: FakeTrack, stream: FakeStream) {
    this.sent.push({ track, stream })
    return { track }
  }
  createDataChannel(label: string): FakeChannel {
    const c = new FakeChannel(label)
    this.channels.push(c)
    return c
  }
  async createOffer(): Promise<RTCSessionDescriptionInit> {
    return { type: 'offer', sdp: 'v=0 fake-offer' }
  }
  async setLocalDescription(d: RTCSessionDescriptionInit): Promise<void> {
    this.local = d
  }
  async setRemoteDescription(d: RTCSessionDescriptionInit): Promise<void> {
    this.remote = d
  }
  close(): void {
    this.closed = true
  }
  /** The agent's audio arriving, as the browser announces a remote track. */
  remoteTrack(stream: FakeStream): void {
    const e = new Event('track') as Event & { track?: FakeTrack; streams?: FakeStream[] }
    e.track = stream.tracks[0]
    e.streams = [stream]
    this.dispatchEvent(e)
  }
  get events(): FakeChannel {
    const c = this.channels[0]
    if (c === undefined) throw new Error('no data channel was opened')
    return c
  }
}

export class FakeAnalyser {
  fftSize = 0
  frequencyBinCount = 1024
  connect = vi.fn()
  disconnect = vi.fn()
  getFloatTimeDomainData = vi.fn((a: Float32Array) => a.fill(0))
}

export class FakeAudioContext {
  static all: FakeAudioContext[] = []
  destination = { node: 'speakers' }
  sources: { stream: FakeStream; connect: ReturnType<typeof vi.fn>; disconnect: ReturnType<typeof vi.fn> }[] = []
  analysers: FakeAnalyser[] = []
  close = vi.fn(async () => undefined)
  constructor() {
    FakeAudioContext.all.push(this)
  }
  createMediaStreamSource(stream: FakeStream) {
    const s = { stream, connect: vi.fn(), disconnect: vi.fn() }
    this.sources.push(s)
    return s
  }
  createAnalyser(): FakeAnalyser {
    const a = new FakeAnalyser()
    this.analysers.push(a)
    return a
  }
}

/** Install the fakes; returns the microphone's track so a test can read `enabled`. */
export function installMedia(opts: { audioContext?: boolean } = {}): { mic: FakeTrack; getUserMedia: ReturnType<typeof vi.fn> } {
  FakePeer.all = []
  FakeAudioContext.all = []
  const mic = new FakeTrack()
  const getUserMedia = vi.fn(async () => new FakeStream([mic]))
  vi.stubGlobal('RTCPeerConnection', FakePeer)
  Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia }, configurable: true })
  if (opts.audioContext) vi.stubGlobal('AudioContext', FakeAudioContext)
  return { mic, getUserMedia }
}

/** Let the start's chain of promises (mic, offer, POST, answer) settle. */
export async function settle(act: (fn: () => Promise<void>) => Promise<void>): Promise<void> {
  for (let i = 0; i < 12; i++) {
    await act(async () => {
      await Promise.resolve()
    })
  }
}
