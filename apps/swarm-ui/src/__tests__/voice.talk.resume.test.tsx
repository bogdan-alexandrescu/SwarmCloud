// VOICE IT, TALK: A RESUMED PROJECT'S CARDS AND MOCK-UPS (the owner's finding
// of 12 November 2026: resuming a project showed nothing on the canvas).
//
//   * when a start takes a resumed project, Talk reads the project's cards and
//     its visuals once and draws its open cards and its mock-up before the
//     session's stream opens;
//   * the stream's later events update those cards in place: a newer version
//     replaces one where it stands, a repeat draws nothing twice;
//   * a card the project closed (done, failed, cancelled, dismissed,
//     accepted) stays out;
//   * a new project reads no project cards and draws none.
//
// MUTATIONS: drop the project cards read; seed closed cards; reset the list
// to empty when the stream opens; append a streamed card that is already
// seeded.

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { installMedia, settle } from './voice.fakes'

const fetchMock = vi.hoisted(() => {
  // The routes, not api.ts's fixtures: fixture mode is decided when api.ts loads.
  vi.stubEnv('VITE_LIVE', '1')
  return { read: vi.fn(), write: vi.fn() }
})

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  USE_FIXTURES: false,
}))

vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  read: fetchMock.read,
  write: fetchMock.write,
}))

import { RESUME_KEY } from '../voice/Projects'
import { FIXTURE_CARDS, type VoiceCard } from '../voice/CardList'
import { TalkPane } from '../voice/Talk'
import { VoiceSessionHolder } from '../voice/session'

const SESSION = 'vs_' + '4'.repeat(32)
const PROJECT = 'vp_' + '5'.repeat(32)
const CARDS_ROUTE = '/v1/voice/projects/{project_id}/cards'

const ok = (data: unknown) => ({ status: 'ok', fetchedAt: Date.now(), data })
const missing = { status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'Not found.' } }

/** An EventSource the test drives: frames carry their sequence as the SSE id. */
class FakeEventSource extends EventTarget {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSED = 2
  static all: FakeEventSource[] = []
  readyState = 1
  onerror: ((e: Event) => void) | null = null
  onopen: ((e: Event) => void) | null = null
  constructor(readonly url: string) {
    super()
    FakeEventSource.all.push(this)
  }
  close(): void {
    this.readyState = 2
  }
  upsert(seq: number, card: VoiceCard): void {
    this.dispatchEvent(new MessageEvent('card.upsert', { data: JSON.stringify({ seq, type: 'card.upsert', card }), lastEventId: String(seq) }))
  }
}

function card(kind: string, id: string, version: number, title: string, state: VoiceCard['state'] = 'waiting'): VoiceCard {
  const base = FIXTURE_CARDS.find((c) => c.kind === kind)
  if (base === undefined) throw new Error(`no fixture card of kind ${kind}`)
  return { ...base, card_id: id, version, title, state, project_id: PROJECT, session_id: 'vs_' + '3'.repeat(32) }
}

const MOCKUP = {
  visual_id: 'vv_page',
  kind: 'mockup',
  title: 'The settings page',
  version: 1,
  source: null,
  parts: [{ id: 'frame-1', label: 'Light' }],
  lines: null,
  created_by: 'planner',
  updated_at: '2026-11-10T09:00:00Z',
  approved: null,
}

/** The project's routes as swarm-api serves them; every other read finds nothing. */
function serveProject(cards: VoiceCard[]): void {
  fetchMock.read.mockImplementation(async (target: { template: string; url: string }) => {
    if (target.url === '/v1/voice/settings') return ok({ settings: { engine: 'gpt-live', debug: false } })
    if (target.template === CARDS_ROUTE) return ok({ cards })
    if (target.template === '/v1/voice/projects/{project_id}/visuals') return ok({ visuals: [MOCKUP] })
    if (target.template === '/v1/voice/projects/{project_id}/visuals/{visual_id}') {
      return ok({ visual: MOCKUP, body: JSON.stringify({ frames: [{ id: 'frame-1', label: 'Light', html: '<h1>Light</h1>' }] }), notes: [] })
    }
    return missing
  })
}

/** The session's start, held until the test lets swarm-api answer. */
function holdStart(project: string | null): () => Promise<void> {
  let answer: (v: unknown) => void = () => undefined
  const answered = new Promise((resolve) => {
    answer = resolve
  })
  fetchMock.write.mockImplementation(async (target: { url: string }) => {
    if (target.url !== '/v1/voice/sessions') return ok(null)
    await answered
    return ok({ session_id: SESSION, project_id: project, answer_sdp: 'v=0 fake-answer' })
  })
  return async () => {
    answer(null)
    await settle(act)
  }
}

async function clickStart(): Promise<void> {
  await settle(act)
  fireEvent.click(screen.getByRole('button', { name: 'Start talking' }))
  await settle(act)
}

function drawn(): string[] {
  const region = screen.getByRole('region', { name: 'Cards' })
  return Array.from(region.querySelectorAll('article')).map((a) => `${a.getAttribute('data-kind')}:${a.getAttribute('data-card-id')}`)
}

const cardReads = () => fetchMock.read.mock.calls.filter(([t]) => t.template === CARDS_ROUTE)

describe('resuming a project opens Talk with its cards and mock-ups', () => {
  beforeEach(() => {
    FakeEventSource.all = []
    vi.stubGlobal('EventSource', FakeEventSource)
    installMedia({ audioContext: true })
    window.sessionStorage.clear()
    fetchMock.read.mockReset()
    fetchMock.write.mockReset()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('draws the two open cards and the mock-up before the stream opens', async () => {
    window.sessionStorage.setItem(RESUME_KEY, PROJECT)
    serveProject([card('work', 'vc_work', 1, 'Settings lane'), card('review', 'vc_review', 2, 'Settings review')])
    const answer = holdStart(PROJECT)
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await clickStart()

    expect(FakeEventSource.all).toHaveLength(0)
    expect(drawn()).toEqual(['work:vc_work', 'review:vc_review'])
    const stage = screen.getByRole('region', { name: 'Stage' })
    expect(within(stage).getByRole('button', { name: /The settings page/ })).toBeTruthy()
    expect(cardReads().map(([t]) => t.url)).toEqual([`/v1/voice/projects/${PROJECT}/cards`])

    await answer()
    expect(FakeEventSource.all.map((e) => e.url)).toEqual([`/v1/voice/sessions/${SESSION}/events`])
    // The stream opening keeps what the project drew, and the project is read once.
    expect(drawn()).toEqual(['work:vc_work', 'review:vc_review'])
    expect(cardReads()).toHaveLength(1)
  })

  it("updates a drawn card in place from the stream's later events, and never draws it twice", async () => {
    window.sessionStorage.setItem(RESUME_KEY, PROJECT)
    serveProject([card('work', 'vc_work', 1, 'Settings lane'), card('review', 'vc_review', 2, 'Settings review')])
    const answer = holdStart(PROJECT)
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await clickStart()
    await answer()
    const es = FakeEventSource.all[0]
    if (es === undefined) throw new Error('the stream did not open')

    // The same version again draws nothing new; a newer one replaces the card where it stands.
    await act(async () => es.upsert(1, card('review', 'vc_review', 2, 'Settings review')))
    expect(drawn()).toEqual(['work:vc_work', 'review:vc_review'])
    await act(async () => es.upsert(2, card('work', 'vc_work', 2, 'Settings lane running')))
    expect(drawn()).toEqual(['work:vc_work', 'review:vc_review'])
    const region = screen.getByRole('region', { name: 'Cards' })
    expect(within(region).getByRole('article', { name: 'Work: Settings lane running' })).toBeTruthy()
    expect(within(region).queryByRole('article', { name: 'Work: Settings lane' })).toBeNull()
  })

  it('draws nothing for a new project, and reads no project cards', async () => {
    serveProject([card('work', 'vc_work', 1, 'Settings lane')])
    const answer = holdStart('vp_' + '6'.repeat(32))
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await clickStart()
    expect(drawn()).toEqual([])
    await answer()
    expect(drawn()).toEqual([])
    expect(cardReads()).toHaveLength(0)
  })

  it('keeps a card the project closed out', async () => {
    window.sessionStorage.setItem(RESUME_KEY, PROJECT)
    serveProject([
      card('work', 'vc_work', 1, 'Settings lane'),
      card('plan', 'vc_done', 3, 'Finished plan', 'done'),
      card('stop', 'vc_dismissed', 1, 'Dismissed stop', 'dismissed'),
    ])
    const answer = holdStart(PROJECT)
    render(<VoiceSessionHolder><TalkPane /></VoiceSessionHolder>)
    await clickStart()
    expect(drawn()).toEqual(['work:vc_work'])
    await answer()
    expect(drawn()).toEqual(['work:vc_work'])
  })
})
