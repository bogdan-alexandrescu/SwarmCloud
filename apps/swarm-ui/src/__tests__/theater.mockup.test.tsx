// THE THEATER MATCHES THE CHOSEN MOCKUP (lane LB-UX2a, part of #1030).
//
// The owner chose "Browser Tab and Theater" (docs/design/live-browser.md §6)
// and, on 2026-10-12, decided item by item to match its mockup. Measured on
// SwarmCloud 2026-10-11/12: prev, next and the other live agents were never
// fed, because the Browser tab passes the theater no list. Each case below is
// one of the brief's items, as behaviour:
//
//  1. With no list passed, the theater reads the tenant's live browser agents
//     itself (the Agents list's Live filter: RUNNING browser agents,
//     needs-you first) and navigates to the chosen one's …/browser?theater=1.
//     A list that IS passed (the workflow graph's live steps) is used as is.
//  2. The labels are the mockup's: "LIVE <agent>", "‹ prev agent", "next ›",
//     "Esc · back to tab"; DEVICE and OTHER LIVE on the right, a device
//     outline per agent. DEVICE shows session.device, or "Not reported" when
//     it is null. The top bar's own chips are gone (QA12-08/24, owner
//     2026-10-13: one set, the DEVICE column's, which switches);
//     theater.followup.test.tsx holds that.
//  3. Replay is a scrubber over the recording's segments, end to end, with
//     "◀◀ replay" and "live ●" -- not a row of segment buttons.
//  4. The hand-off checklist ticks what is done and its green "I'm done,
//     return control" returns control exactly as the bottom bar does.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { useEffect, useRef, useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LiveSession } from '../api'
import type { Task } from '../types'

// The live path, not the development fixtures: USE_FIXTURES is decided when
// api.ts is imported, so the environment is set before the modules load.
vi.stubEnv('VITE_LIVE', '1')
const { TheaterHost } = await import('../Theater')
type TheaterSession = import('../Theater').TheaterSession
type LiveAgentRef = import('../Theater').LiveAgentRef
const { theaterPath } = await import('../LiveThumb')

const TENANT = 'eng'
const TASK = 'task_8cf26eb6e1f94e85adf8'
const FIXTURES = join(__dirname, '..', '..', '..', '..', 'tests', 'fixtures', 'live')

function fixture<T>(file: string): T {
  return JSON.parse(readFileSync(join(FIXTURES, file), 'utf8')) as T
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

interface Call {
  url: string
  method: string
}

function stubFetch(answer: (url: string, method: string) => Response): Call[] {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      calls.push({ url, method })
      return answer(url, method)
    }),
  )
  return calls
}

function row(id: string, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: TENANT,
    state: 'RUNNING',
    runner_profile: 'claude-code-browser',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 5,
    created_at: '2026-10-12T10:00:00Z',
    updated_at: '2026-10-12T10:01:00Z',
    started_at: '2026-10-12T10:00:30Z',
    completed_at: null,
    submitted_by: 'alice@example.com',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    ...over,
  } as Task
}

/** The tenant's page: this agent, one waiting for a person, one plain browser, and two that are not live browsers. */
const PAGE = {
  tasks: [
    row(TASK, { step_id: 'this-one' }),
    row('task_plain_browser_000001', { step_id: 'plain-browser' }),
    row('task_waiting_browser_0001', {
      step_id: 'needs-a-person',
      human_wait: { reason: 'Sign in', expect: 'github.com', deadline: '2026-10-12T10:20:00Z' },
    } as Partial<Task>),
    row('task_not_a_browser_00001', { step_id: 'ordinary', runner_profile: 'claude-code' }),
    row('task_finished_browser_01', { step_id: 'finished', state: 'SUCCEEDED' }),
  ],
  next_page_token: null,
}

const OPEN = fixture<{ session: LiveSession }>('session.open.json').session
const RECORDINGS = fixture<Record<string, unknown>>('recordings.json')

function answer(url: string, method: string): Response {
  if (url.startsWith('/v1/tasks?')) return json(PAGE)
  if (url.includes('/live/actions')) return json({ attempt_id: 'att_x', actions: [] })
  if (url.startsWith(`/v1/tasks/${TASK}/live/recordings`)) return json(RECORDINGS)
  if (url.startsWith(`/v1/tasks/${TASK}/live/control`) && method === 'DELETE') return json(OPEN)
  return json({ code: 'not_found', message: 'Not found.' }, 404)
}

function Tab(props: {
  agents?: LiveAgentRef[]
  onNavigate?: (taskId: string) => void
  browser?: LiveSession | null
  reload?: () => void
  handoff?: import('../Theater').TheaterHandoff | null
}) {
  const screenRef = useRef<HTMLDivElement>(null)
  const [node, setNode] = useState<HTMLElement | null>(null)
  useEffect(() => setNode(screenRef.current), [])
  const session: TheaterSession = {
    taskId: TASK,
    tenantId: TENANT,
    attemptId: '',
    sessionId: OPEN.session_id,
    screen: node,
    live: true,
    browser: props.browser === undefined ? OPEN : props.browser,
    reload: props.reload,
  }
  return (
    <TheaterHost
      session={session}
      agents={props.agents}
      onNavigate={props.onNavigate}
      handoff={props.handoff ?? null}
      control={<button type="button">Take control</button>}
      initiallyOpen
    >
      <div ref={screenRef} data-live-screen="" />
    </TheaterHost>
  )
}

async function flush(): Promise<void> {
  for (let i = 0; i < 4; i += 1) {
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
  }
}

beforeEach(() => {
  window.history.replaceState(null, '', '/')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('1. prev, next and OTHER LIVE are fed', () => {
  it('with no list passed, reads the tenant’s live browser agents, needs-you first, and lists the others', async () => {
    const calls = stubFetch(answer)
    render(<Tab />)
    await flush()

    // The same read the Agents list's Live filter is drawn from.
    const read = calls.find((c) => c.url.startsWith('/v1/tasks?'))
    expect(read, 'the theater read no task page').toBeDefined()
    expect(new URLSearchParams(read!.url.split('?')[1]).get('view')).toBe('summary')

    const others = within(screen.getByRole('region', { name: 'OTHER LIVE' }))
    const names = others.getAllByRole('listitem').map((li) => li.textContent ?? '')
    expect(names.length).toBe(2)
    expect(names[0]).toContain('needs-a-person')
    expect(names[1]).toContain('plain-browser')
    for (const n of names) {
      expect(n).not.toContain('ordinary')
      expect(n).not.toContain('finished')
      expect(n).not.toContain('this-one')
    }
    // The theater names this agent by the list's name for it.
    expect(screen.getByTestId('theater-live-pill').textContent).toContain('this-one')
  })

  it('next › navigates to the chosen agent’s …/browser?theater=1 through the router', async () => {
    stubFetch(answer)
    const pops = vi.fn()
    window.addEventListener('popstate', pops)
    render(<Tab />)
    await flush()

    // needs-a-person, this-one, plain-browser: prev is the waiting one, next the plain one.
    fireEvent.click(screen.getByRole('button', { name: 'next ›' }))
    expect(`${window.location.pathname}${window.location.search}`).toBe(theaterPath('task_plain_browser_000001', null))
    expect(pops).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: '‹ prev agent' }))
    expect(`${window.location.pathname}${window.location.search}`).toBe(theaterPath('task_waiting_browser_0001', null))

    fireEvent.click(within(screen.getByRole('region', { name: 'OTHER LIVE' })).getByRole('button', { name: /plain-browser/ }))
    expect(`${window.location.pathname}${window.location.search}`).toBe(theaterPath('task_plain_browser_000001', null))
    window.removeEventListener('popstate', pops)
  })

  it('a list that is passed (the workflow graph’s live steps) is walked as given, and no page is read', async () => {
    const calls = stubFetch(answer)
    const onNavigate = vi.fn()
    const agents: LiveAgentRef[] = [
      { taskId: 'task_step_one', label: 'step-one', needsYou: false },
      { taskId: TASK, label: 'this-step', needsYou: false },
      { taskId: 'task_step_three', label: 'step-three', needsYou: false },
    ]
    render(<Tab agents={agents} onNavigate={onNavigate} />)
    await flush()
    expect(calls.some((c) => c.url.startsWith('/v1/tasks?'))).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'next ›' }))
    expect(onNavigate).toHaveBeenLastCalledWith('task_step_three')
  })
})

describe('2. the mockup’s labels', () => {
  it('top bar: LIVE pill, prev, next and Esc · back to tab, and no second set of device chips; right: DEVICE and OTHER LIVE', async () => {
    stubFetch(answer)
    const reported = { ...OPEN, device: { preset: 'iphone-15', orientation: 'portrait' as const, touch: true } }
    render(<Tab browser={reported} />)
    await flush()

    const pill = screen.getByTestId('theater-live-pill')
    expect(pill.textContent).toMatch(/^LIVE\s*this-one$/)

    // The device chips are the DEVICE column's (the tab's slot), never the top bar's.
    expect(within(screen.getByRole('dialog').querySelector('header')!).queryByRole('group', { name: 'Device' })).toBeNull()

    for (const name of ['‹ prev agent', 'next ›', 'Esc · back to tab']) {
      expect(screen.getByRole('button', { name })).toBeTruthy()
    }
    const device = screen.getByRole('region', { name: 'DEVICE' })
    expect(device.textContent).toContain('iPhone 15')
    expect(device.textContent).toContain('portrait')
    const others = screen.getByRole('region', { name: 'OTHER LIVE' })
    expect(others.querySelectorAll('[data-device-outline]').length).toBe(2)
  })

  it('a device nobody reported reads "Not reported", with the unknown outline', async () => {
    stubFetch(answer)
    render(<Tab />)
    await flush()
    const device = screen.getByRole('region', { name: 'DEVICE' })
    expect(device.textContent).toContain('Not reported')
    expect(device.querySelector('[data-device-outline]')?.getAttribute('data-device-outline')).toBe('unknown')
  })

  it('Esc · back to tab closes the theater', async () => {
    stubFetch(answer)
    render(<Tab />)
    await flush()
    fireEvent.click(screen.getByRole('button', { name: 'Esc · back to tab' }))
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

describe('3. replay is a scrubber over the recording', () => {
  it('lays the segments end to end, plays from ◀◀ replay, seeks by the bar, and live ● goes back', async () => {
    const calls = stubFetch(answer)
    render(<Tab />)
    await flush()

    const bar = screen.getByRole('slider', { name: 'Recording' }) as HTMLInputElement
    expect(screen.getByTestId('theater-scrubber').querySelectorAll('[data-segment]').length).toBe(2)
    // No row of per-segment buttons any more.
    expect(screen.queryAllByRole('button', { name: /^segment /i })).toEqual([])
    expect(screen.queryByTestId('theater-replay')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '◀◀ replay' }))
    await flush()
    let video = screen.getByTestId('theater-replay')
    expect(video.getAttribute('src')!.split('?')[0]).toBe(`/v1/tasks/${TASK}/attempts/att_fafe0d6de50c4ff39de9/live/recordings/0`)

    // Into the second minute: the second segment.
    fireEvent.change(bar, { target: { value: String(Number(bar.max) * 0.75) } })
    await flush()
    video = screen.getByTestId('theater-replay')
    expect(video.getAttribute('src')!.split('?')[0]).toBe(`/v1/tasks/${TASK}/attempts/att_fafe0d6de50c4ff39de9/live/recordings/1`)

    fireEvent.click(screen.getByRole('button', { name: 'live ●' }))
    expect(screen.queryByTestId('theater-replay')).toBeNull()
    // The listing was read once, through the task route.
    expect(calls.filter((c) => c.url.startsWith(`/v1/tasks/${TASK}/live/recordings`)).length).toBe(1)
  })
})

describe('4. the hand-off checklist', () => {
  const handoff = { reason: 'Sign in to GitHub', expect: 'github.com', deadline: new Date(Date.now() + 300_000).toISOString() }

  it('ticks the steps done and returns control with the same action as the bottom bar', async () => {
    const calls = stubFetch(answer)
    const reload = vi.fn()
    const mine = { ...OPEN, controller: { email: OPEN.you.email, since: '2026-10-12T10:05:00Z' } }
    render(<Tab browser={mine} reload={reload} handoff={handoff} />)
    await flush()

    const list = screen.getByRole('region', { name: 'Hand-off checklist' })
    const done = [...list.querySelectorAll('[data-done="true"]')].map((li) => li.textContent ?? '')
    expect(done.length).toBe(2)
    expect(done[0]).toMatch(/asked for your help/i)
    expect(done[0]).toContain('✓')
    expect(done[1]).toMatch(/you took control/i)

    const back = within(list).getByRole('button', { name: "I'm done, return control" }) as HTMLButtonElement
    expect(back.disabled).toBe(false)
    fireEvent.click(back)
    await flush()
    const ret = calls.filter((c) => c.method === 'DELETE')
    expect(ret.map((c) => c.url.split('?')[0])).toEqual([`/v1/tasks/${TASK}/live/control`])
    expect(reload).toHaveBeenCalled()
  })

  it('before you take control, that step is not ticked and the return button waits', async () => {
    stubFetch(answer)
    render(<Tab handoff={handoff} />)
    await flush()
    const list = screen.getByRole('region', { name: 'Hand-off checklist' })
    expect(list.querySelectorAll('[data-done="true"]').length).toBe(1)
    const back = within(list).getByRole('button', { name: "I'm done, return control" }) as HTMLButtonElement
    expect(back.disabled).toBe(true)
  })
})
