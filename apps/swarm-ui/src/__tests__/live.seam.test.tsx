// THE LIVE ROUTES' WIRE, FROM THE CONSOLE'S END (lane LB-SEAM, part of #1030).
//
// MEASURED 2026-10-11 on SwarmCloud: a claude-code-browser task had a working
// live path and `GET /v1/tasks/{id}/live/session` answered 200 -- in a shape
// with no `live_browser` and no `session`, so `useLiveSession` read
// `live_browser === true` as false and the agent drew no Browser tab. Lanes L9
// (API) and L11 (this console) were built in parallel against different
// shapes, and the only seam check compared paths.
//
// tests/fixtures/live/ holds the wire once. The API's half
// (tests/unit/control_plane/test_live_wire_shapes.py) validates every file
// with the route's response model and compares the real routes' answers with
// them. This half sends the SAME files through the console's own loaders --
// the real `loadLiveSession`, `mintLiveTicket`, `takeLiveControl` and the
// rest, over a stubbed `fetch` -- and asserts what the Browser tab is drawn
// from. `index.json` lists the routes; one this file has no case for fails.
//
// MUTATION: rename `live_browser` in session.open.json (or in the API's
// model) and both halves fail: the hook below reads no Browser tab, and the
// pytest model refuses the file.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { LiveRead, LiveTicket } from '../api'
import type { Result } from '../fetch'
import type { LiveActions } from '../TheaterTimeline'

vi.mock('@novnc/novnc/core/rfb.js', () => ({ default: class {} }))

// The live path, not the development fixtures: USE_FIXTURES is decided when
// api.ts is imported, so the environment is set first and the modules are
// imported after it.
vi.stubEnv('VITE_LIVE', '1')
const {
  closeLiveToMembers,
  loadLiveRecordings,
  loadLiveSession,
  mintLiveTicket,
  openLiveToMembers,
  renewLiveControl,
  returnLiveControl,
  setLiveDevice,
  takeLiveControl,
} = await import('../api')
const { useLiveSession } = await import('../LiveBrowser')
const { segmentsOfTenant } = await import('../Theater')
const { actionWords, loadLiveActions } = await import('../TheaterTimeline')
const { ownRecording } = await import('../Recordings')
const { openLiveSocket: openThumbSocket } = await import('../LiveThumb')

const FIXTURES = join(__dirname, '..', '..', '..', '..', 'tests', 'fixtures', 'live')
const TASK = 'task_8cf26eb6e1f94e85adf8'

interface Row {
  file: string
  method: string
  path: string
  direction: 'request' | 'response'
  model: string
  ui: string
}

const INDEX: Row[] = (JSON.parse(readFileSync(join(FIXTURES, 'index.json'), 'utf8')) as { fixtures: Row[] }).fixtures

function fixture<T = unknown>(file: string): T {
  return JSON.parse(readFileSync(join(FIXTURES, file), 'utf8')) as T
}

function okJson(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

/** Serve `body` to every request and record what was asked. */
function serve(body: unknown) {
  const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => okJson(body))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function data<T>(r: Result<T>): T {
  expect(r.status, JSON.stringify(r)).toBe('ok')
  return (r as { data: T }).data
}

/** The console's call for each loader the index names, with the fixture as the server's answer. */
const CALLS: Record<string, (body: unknown) => Promise<Result<unknown>>> = {
  loadLiveSession: () => loadLiveSession(TASK),
  mintLiveTicket: () => mintLiveTicket(TASK),
  takeLiveControl: (body) => takeLiveControl(TASK, (body ?? {}) as Parameters<typeof takeLiveControl>[1]),
  renewLiveControl: () => renewLiveControl(TASK),
  returnLiveControl: () => returnLiveControl(TASK),
  openLiveToMembers: () => openLiveToMembers(TASK),
  closeLiveToMembers: () => closeLiveToMembers(TASK),
  setLiveDevice: (body) =>
    setLiveDevice(TASK, (body ?? { preset: 'desktop' }) as Parameters<typeof setLiveDevice>[1]),
  // The Attempts tab's player and the theater's replay share this one loader (LB-UX2a).
  loadRecordings: () => loadLiveRecordings(TASK),
  loadLiveActions: () => loadLiveActions(TASK),
}

describe('every route in the shared index is one the console calls, at that method and path', () => {
  it.each(INDEX.map((r) => [`${r.method} ${r.path} ${r.file}`, r] as const))('%s', async (_name, row) => {
    const call = CALLS[row.ui]
    expect(call, `the seam test has no case for ${row.ui}`).toBeDefined()
    const fetchMock = serve(row.direction === 'response' ? fixture(row.file) : {})
    await call!(row.direction === 'request' ? fixture(row.file) : undefined)
    const [url, init] = fetchMock.mock.calls[0]!
    expect(String(url).split('?')[0]).toBe(row.path.replace('{task_id}', TASK))
    expect((init?.method ?? 'GET').toUpperCase()).toBe(row.method)
  })
})

describe('the session read, as the API serves it, draws a Browser tab', () => {
  it('session.open.json: live_browser true leads to a Browser tab and an open session this person may control', async () => {
    serve(fixture('session.open.json'))
    const read = data(await loadLiveSession(TASK))
    expect(read.live_browser).toBe(true)
    const s = read.session!
    expect(s.state).toBe('open')
    expect(s.session_id).toMatch(/^live_att_/)
    expect(s.you.can_control).toBe(true)
    expect(s.you.can_preempt).toBe(false)
    expect(s.you.can_open).toBe(true)
    // Nobody is in control, so nobody switches the device (LB-DEV-B).
    expect(s.you.can_switch_device).toBe(false)
    expect(s.you.email).toBe('alice@saga.xyz')
    expect(s.controller).toBeNull()
    expect(s.human_wait?.expect).toBe('github.com')
    // Not reported to swarm-api yet: null, and the console must draw that.
    expect(s.origin).toBeNull()
    expect(s.device).toBeNull()
    expect(s.presets.map((p) => p.name)).toEqual(['desktop', 'iphone-15', 'pixel-8', 'iphone-se', 'ipad-7'])

    // `hasBrowser` in AgentSplit is exactly `status === 'ok' && liveBrowser`.
    const { result } = renderHook(() => useLiveSession(TASK, true))
    await waitFor(() => expect(result.current.live.status).toBe('ok'))
    const live = result.current.live as { status: 'ok'; liveBrowser: boolean; session: LiveRead['session'] }
    expect(live.liveBrowser).toBe(true)
    expect(live.session?.session_id).toBe(s.session_id)
  })

  it('session.reported.json: a reported origin, device and controller are read as such', async () => {
    serve(fixture('session.reported.json'))
    const s = data(await loadLiveSession(TASK)).session!
    expect(s.origin?.registrable_domain).toBe('github.com')
    expect(s.device).toEqual({ preset: 'iphone-15', orientation: 'portrait', touch: true })
    expect(s.controller?.email).toBe('dave@saga.xyz')
    expect(s.you.can_preempt).toBe(true)
    // dave holds control, not alice: her device bar stays disabled.
    expect(s.you.can_switch_device).toBe(false)
    expect(s.open_to_members).toBe(true)
  })

  it('session.pending.json: a live-browser task whose tunnel is not up yet still draws a Browser tab', async () => {
    serve(fixture('session.pending.json'))
    const { result } = renderHook(() => useLiveSession(TASK, true))
    await waitFor(() => expect(result.current.live.status).toBe('ok'))
    expect(result.current.live).toEqual({ status: 'ok', liveBrowser: true, session: null })
  })

  it('session.not-live.json: another profile draws none', async () => {
    serve(fixture('session.not-live.json'))
    const { result } = renderHook(() => useLiveSession(TASK, true))
    await waitFor(() => expect(result.current.live.status).toBe('ok'))
    expect(result.current.live).toEqual({ status: 'ok', liveBrowser: false, session: null })
  })

  it('session.closed.json: a closed session says why', async () => {
    serve(fixture('session.closed.json'))
    const s = data(await loadLiveSession(TASK)).session!
    expect(s.state).toBe('closed')
    expect(s.closed_reason).toBe('cancelled')
    expect(s.you.can_control).toBe(false)
  })

  it('the old answer (no live_browser) reads as no Browser tab: the defect this file exists for', async () => {
    serve({ available: true, session_id: 'live_att_x', state: 'open', you: { may_take: true } })
    const { result } = renderHook(() => useLiveSession(TASK, true))
    await waitFor(() => expect(result.current.live.status).toBe('ok'))
    expect((result.current.live as { liveBrowser: boolean }).liveBrowser).toBe(false)
  })
})

describe('the control and ticket routes', () => {
  it('ticket.json: the ticket, its session and the socket path are read', async () => {
    serve(fixture('ticket.json'))
    const t = data<LiveTicket>(await mintLiveTicket(TASK))
    expect(t.session_id).toMatch(/^live_att_/)
    expect(t.socket_path).toBe(`/v1/live/${t.session_id}/stream`)
    expect(t.expires_in).toBe(30)
    expect(typeof t.ticket).toBe('string')
  })

  it('control.take.request.json: takeLiveControl sends exactly the body the API model accepts', async () => {
    const fetchMock = serve(fixture('control.taken.json'))
    const body = fixture<Parameters<typeof takeLiveControl>[1]>('control.take.request.json')
    const r = await takeLiveControl(TASK, body)
    expect(r.status).toBe('ok')
    const init = fetchMock.mock.calls[0]![1]!
    expect(JSON.parse(String(init.body))).toEqual(fixture('control.take.request.json'))
    const taken = data(r) as LiveRead
    expect(taken.session?.controller?.email).toBe('alice@saga.xyz')
  })

  it('device.request.json: setLiveDevice sends exactly the body the API model accepts, a preset by name', async () => {
    const fetchMock = serve(fixture('device.switched.json'))
    const body = fixture<Parameters<typeof setLiveDevice>[1]>('device.request.json')
    const r = await setLiveDevice(TASK, body)
    expect(r.status).toBe('ok')
    const [url, init] = fetchMock.mock.calls[0]!
    expect(String(url).split('?')[0]).toBe(`/v1/tasks/${TASK}/live/device`)
    expect(init?.method).toBe('POST')
    const sent = JSON.parse(String(init!.body)) as Record<string, unknown>
    expect(sent).toEqual(fixture('device.request.json'))
    // Invariant 10: a name, never a size.
    expect(Object.keys(sent).sort()).toEqual(['orientation', 'preset', 'touch'])
  })

  it('device.switched.json: the controller may switch, and the device is what the gate reported', async () => {
    serve(fixture('device.switched.json'))
    const s = data(await loadLiveSession(TASK)).session!
    expect(s.controller?.email).toBe('alice@saga.xyz')
    expect(s.you.can_switch_device).toBe(true)
    expect(s.device).toEqual({ preset: 'iphone-15', orientation: 'landscape', touch: false })
  })

  it('control.taken.json: the person who took control may switch the device', async () => {
    serve(fixture('control.taken.json'))
    const s = data(await loadLiveSession(TASK)).session!
    expect(s.you.can_switch_device).toBe(true)
  })

  it('returnLiveControl is DELETE on .../live/control, which the API now serves', async () => {
    const fetchMock = serve(fixture('session.open.json'))
    expect((await returnLiveControl(TASK)).status).toBe('ok')
    const [url, init] = fetchMock.mock.calls[0]!
    expect(String(url).split('?')[0]).toBe(`/v1/tasks/${TASK}/live/control`)
    expect(init?.method).toBe('DELETE')
  })
})

describe('recordings and the action timeline (§7)', () => {
  it('recordings.json: the theater keeps every segment the relay wrote, and the list keeps every recording', async () => {
    const listing = fixture<{ tenant_id: string; task_id: string; recordings: Parameters<typeof ownRecording>[1][] }>('recordings.json')
    serve(listing)
    const read = await loadLiveRecordings(TASK)
    expect(read.status).toBe('ok')
    const data = (read as { data: Parameters<typeof segmentsOfTenant>[0] }).data
    // The relay's real names are zero-padded to 13 digits; an unpadded
    // comparison refused every one of them.
    const { kept, refused } = segmentsOfTenant(data, { taskId: listing.task_id, tenantId: listing.tenant_id })
    expect(refused).toBe(0)
    expect(kept.map((s) => s.seq)).toEqual([0, 1])
    const task = { id: listing.task_id, tenant_id: listing.tenant_id }
    expect(listing.recordings.every((r) => ownRecording(task, r))).toBe(true)
  })

  it('actions.json: the timeline reads methods, lengths, refusals and what people did', async () => {
    serve(fixture('actions.json'))
    const read = await loadLiveActions(TASK)
    expect(read.status).toBe('ok')
    const { actions, attempt_id } = (read as { data: LiveActions }).data
    expect(attempt_id).toMatch(/^att_/)
    expect(actions.find((a) => a.method === 'Input.insertText')!.length).toBe(12)
    // LB-V3: who took control, the device and its rotation, and why a refusal.
    const taken = actions.find((a) => a.event === 'control_taken')!
    expect(taken.by).toMatch(/@/)
    expect(taken.you).toBe(true)
    const device = actions.find((a) => a.event === 'device_changed')!
    expect([device.preset, device.orientation, device.touch]).toEqual(['iphone-15', 'portrait', true])
    expect(actions.find((a) => a.event === 'rotated')!.orientation).toBe('landscape')
    expect(actions.find((a) => a.event === 'refused')!.cause).toBe('insecure_connection')
    expect(actions.find((a) => a.outcome === 'refused')!.cause).toBe('method_policy')
    expect(actions.map((a) => actionWords(a))).toContain('You rotated the iPhone 15 to landscape')
  })
})

describe('the workflow card thumbnail opens the ticketed stream (§5.2)', () => {
  it('reads the session, mints a ticket, opens /v1/live/<session>/stream and sends the ticket before any RFB byte', async () => {
    const session = fixture<LiveRead>('session.open.json')
    const ticket = fixture<LiveTicket>('ticket.json')
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => okJson(String(url).includes('/live/ticket') ? ticket : session)),
    )
    const made: { url: string; sent: unknown[]; ws: Record<string, unknown> }[] = []
    class FakeWs {
      readyState = 0
      binaryType = 'blob'
      onopen: (() => void) | null = null
      onmessage: ((e: { data: unknown }) => void) | null = null
      onclose: ((e: unknown) => void) | null = null
      onerror: ((e: unknown) => void) | null = null
      readonly sent: unknown[] = []
      constructor(readonly url: string) {
        made.push({ url, sent: this.sent, ws: this as unknown as Record<string, unknown> })
      }
      send(d: unknown) {
        this.sent.push(d)
      }
      close() {}
    }
    vi.stubGlobal('WebSocket', FakeWs)
    const sock = openThumbSocket(TASK)!
    const got: unknown[] = []
    sock.onmessage = (e) => got.push(e.data)
    const early = new Uint8Array([1, 2, 3])
    sock.send(early)
    await waitFor(() => expect(made.length).toBe(1))
    const ws = made[0]!.ws as unknown as FakeWs
    expect(new URL(made[0]!.url).pathname).toBe(`/v1/live/${ticket.session_id}/stream`)
    expect(new URL(made[0]!.url).search).toBe('')
    ws.readyState = 1
    ws.onopen?.()
    expect(made[0]!.sent[0]).toBe(JSON.stringify({ ticket: ticket.ticket }))
    expect(made[0]!.sent[1]).toBe(early)
    // swarm-api's status text is not RFB; binary is.
    ws.onmessage?.({ data: '{"type":"status","open":true}' })
    const frame = new ArrayBuffer(4)
    ws.onmessage?.({ data: frame })
    expect(got).toEqual([frame])
  })

  it('a task with no open session closes without opening a socket', async () => {
    serve(fixture('session.pending.json'))
    const opened = vi.fn()
    vi.stubGlobal('WebSocket', opened)
    const sock = openThumbSocket(TASK)!
    const closed = new Promise((resolve) => (sock.onclose = resolve))
    await closed
    expect(opened).not.toHaveBeenCalled()
  })
})
