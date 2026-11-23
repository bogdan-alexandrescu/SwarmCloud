// VOICE IT › ONE SESSION, the console's two calls: the transcript
// (`GET /v1/voice/sessions/{id}/transcript`, text/plain as an attachment) and
// the summary (`GET /v1/voice/sessions/{id}/summary`, JSON).
//
//   * the transcript is read with the api layer's headers (the chosen
//     tenant's `X-Swarm-Tenant`) and the IAP cookie, and handed back as a
//     Blob with the file name the server's Content-Disposition gave;
//   * a refusal is an error result with its classified kind, and a sign-in
//     page in place of the file is an expired sign-in, never a transcript;
//   * the summary keeps the served fields, and a state it does not know is
//     "none", so nothing is drawn for it.
//
// BREAK IT: drop the tenant header, save the sign-in page as the transcript,
// or pass an unknown summary state through.

import { afterEach, describe, expect, it, vi } from 'vitest'

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

/** The live path: USE_FIXTURES is decided at import. */
async function live() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const fetchMod = await import('../fetch')
  const api = await import('../api')
  return { api, fetchMod }
}

interface Sent {
  url: string
  init: RequestInit | undefined
}

function serve(answer: (url: string) => Response): Sent[] {
  const sent: Sent[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    sent.push({ url, init })
    return answer(url)
  }))
  return sent
}

/** A Blob's text through FileReader: jsdom's Blob has no `text()`. */
function textOf(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(reader.error)
    reader.readAsText(blob)
  })
}

afterEach(() => {
  localStorage.clear()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('loadVoiceTranscript', () => {
  it('reads the transcript with the tenant header and hands back the file the server named', async () => {
    const { api, fetchMod } = await live()
    fetchMod.chooseTenant('ws-one')
    const sent = serve(
      () =>
        new Response('person: hello\nvoice: hi\n', {
          status: 200,
          headers: { 'content-type': 'text/plain; charset=utf-8', 'content-disposition': 'attachment; filename="voice-vs_0001-transcript.txt"' },
        }),
    )
    const r = await api.loadVoiceTranscript('vs_0001')
    expect(sent).toHaveLength(1)
    expect(sent[0]!.url).toContain('/v1/voice/sessions/vs_0001/transcript')
    const headers = new Headers(sent[0]!.init?.headers)
    expect(headers.get(fetchMod.TENANT_HEADER)).toBe('ws-one')
    expect(sent[0]!.init?.credentials).toBe('same-origin')
    expect(r.status).toBe('ok')
    if (r.status !== 'ok') return
    expect(r.data.filename).toBe('voice-vs_0001-transcript.txt')
    expect(await textOf(r.data.blob)).toBe('person: hello\nvoice: hi\n')
  })

  it('names the file after the session when the server names none, and never takes a path from it', async () => {
    const { api } = await live()
    serve(() => new Response('x', { status: 200, headers: { 'content-type': 'text/plain' } }))
    const plain = await api.loadVoiceTranscript('vs_0001')
    expect(plain.status === 'ok' && plain.data.filename).toBe('voice-vs_0001-transcript.txt')
    serve(() => new Response('x', { status: 200, headers: { 'content-type': 'text/plain', 'content-disposition': 'attachment; filename="../../etc/t.txt"' } }))
    const pathy = await api.loadVoiceTranscript('vs_0001')
    expect(pathy.status === 'ok' && pathy.data.filename).toBe('t.txt')
  })

  it('answers a refusal with its classified error, never a file', async () => {
    const { api } = await live()
    serve(() => json(404, { code: 'not_found', message: 'No such session.' }))
    const r = await api.loadVoiceTranscript('vs_gone')
    expect(r.status).toBe('error')
    if (r.status !== 'error') return
    expect(r.error.kind).toBe('not_found')
    expect(r.error.httpStatus).toBe(404)
    expect(r.error.message).toBe('No such session.')
  })

  it('answers a sign-in page in place of the transcript as an expired sign-in', async () => {
    const { api } = await live()
    serve(() => new Response('<html>sign in</html>', { status: 200, headers: { 'content-type': 'text/html' } }))
    const r = await api.loadVoiceTranscript('vs_0001')
    expect(r.status === 'error' && r.error.kind).toBe('session_expired')
  })

  it('answers a request that never arrived as unreachable', async () => {
    const { api } = await live()
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new TypeError('Failed to fetch')
    }))
    const r = await api.loadVoiceTranscript('vs_0001')
    expect(r.status === 'error' && r.error.kind).toBe('unreachable')
  })
})

describe('loadVoiceSessionSummary', () => {
  it('reads the summary route and keeps the served fields', async () => {
    const { api } = await live()
    const sent = serve(() => json(200, { session_id: 'vs_0001', state: 'written', text: 'Planned the export page.', at: '2026-11-20T10:00:00+00:00' }))
    const r = await api.loadVoiceSessionSummary('vs_0001')
    expect(sent[0]!.url).toContain('/v1/voice/sessions/vs_0001/summary')
    expect(r.status === 'ok' && r.data).toEqual({ session_id: 'vs_0001', state: 'written', text: 'Planned the export page.', at: '2026-11-20T10:00:00+00:00' })
  })

  it('keeps every served state and reads one it does not know as none', async () => {
    const { api } = await live()
    for (const state of ['written', 'pending', 'capped', 'failed', 'none']) {
      serve(() => json(200, { session_id: 'vs_0001', state, text: null, at: null }))
      const r = await api.loadVoiceSessionSummary('vs_0001')
      expect(r.status === 'ok' && r.data.state).toBe(state)
    }
    serve(() => json(200, { session_id: 'vs_0001', state: 'rewritten', text: 'x', at: null }))
    const odd = await api.loadVoiceSessionSummary('vs_0001')
    expect(odd.status === 'ok' && odd.data.state).toBe('none')
  })

  it('answers a failed read with an error, never a summary', async () => {
    const { api } = await live()
    serve(() => json(500, { message: 'boom' }))
    const r = await api.loadVoiceSessionSummary('vs_0001')
    expect(r.status).toBe('error')
  })
})
