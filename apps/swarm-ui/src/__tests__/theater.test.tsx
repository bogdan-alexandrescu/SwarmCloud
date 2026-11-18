// Theater mode (LB-L11b, docs/design/live-browser.md §6, LB-5).
//
// The row's acceptance, each as behaviour rather than source text:
//
//  * ⤢ and `T` open the theater ON THE SAME SESSION. The tab owns the one
//    tunnel and the one viewer ticket; the theater borrows the tab's screen
//    node. So: the very node the tab rendered is the node in the stage, no
//    second WebSocket is constructed, and no ticket-shaped route is read.
//  * Esc returns to the tab: the screen node goes home, the scroll the tab was
//    at comes back, and focus returns to what opened it.
//  * Timeline and Logs switch in the left column.
//  * prev and next walk the live agents in order (needs-you first, §6).
//  * replay reads only the caller's tenant's segments: the listing is read
//    through the tenant-scoped task route, and a segment whose object is not
//    under `tenants/<the task's tenant>/tasks/<task>/attempts/<a>/live/` with
//    the recording suffix is never offered and never requested.
//
// WHERE THIS FILE IS. The build plan names `apps/swarm-ui/test/Theater.test.tsx`;
// vitest.config.ts includes only `src/**/*.test.tsx`, and `test/` holds the
// node runner's `*.test.mjs`, so a file there would never run. A test that
// never runs is a pass by construction, so it lives where the runner looks.

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { useEffect, useRef, useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { TENANT_HEADER, TENANT_PREF } from '../fetch'
import {
  TheaterButton,
  TheaterHost,
  orderLiveAgents,
  segmentsOfTenant,
  type LiveAgentRef,
  type RecordingListing,
  type TheaterSession,
} from '../Theater'

const TENANT = 'eng'
const TASK = 'task_live_one'
const ATTEMPT = 'att_live_one'

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

/** Every URL fetched, with the tenant header it carried. */
interface Call {
  url: string
  tenant: string | null
}

function stubFetch(answer: (url: string) => Response): Call[] {
  const calls: Call[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const headers = (init?.headers ?? {}) as Record<string, string>
    calls.push({ url, tenant: headers[TENANT_HEADER] ?? null })
    return answer(url)
  }) as unknown as typeof fetch
  return calls
}

const NO_ACTIONS = (url: string): Response =>
  url.includes('/live/actions') ? json({ attempt_id: ATTEMPT, actions: [] }) : json({ detail: 'Not Found' }, 404)

/** Counts every WebSocket the page constructs. */
let sockets = 0
class CountingSocket {
  constructor() {
    sockets += 1
  }
  close(): void {}
  send(): void {}
  addEventListener(): void {}
  removeEventListener(): void {}
}

beforeEach(() => {
  sockets = 0
  vi.stubGlobal('WebSocket', CountingSocket)
})

afterEach(() => {
  try {
    sessionStorage.removeItem(TENANT_PREF)
  } catch {
    /* storage refused */
  }
})

/**
 * A stand-in for L11's Browser tab: it opens ONE socket on mount, as the tab
 * does for its noVNC connection, and renders the screen node noVNC draws into.
 * Only the theater is under test; this harness is in the test, never in src.
 */
function Tab(props: {
  taskId?: string
  agents?: LiveAgentRef[]
  onNavigate?: (taskId: string) => void
  initiallyOpen?: boolean
}) {
  const screenRef = useRef<HTMLDivElement>(null)
  const [node, setNode] = useState<HTMLElement | null>(null)
  useEffect(() => {
    // eslint-disable-next-line no-new
    new WebSocket('wss://console.invalid/v1/tasks/' + (props.taskId ?? TASK) + '/live')
    setNode(screenRef.current)
  }, [props.taskId])
  const session: TheaterSession = {
    taskId: props.taskId ?? TASK,
    tenantId: TENANT,
    attemptId: ATTEMPT,
    sessionId: 'sess_one',
    screen: node,
    live: true,
  }
  return (
    <div data-testid="pane" style={{ overflow: 'auto' }}>
      <TheaterHost
        session={session}
        agents={props.agents}
        onNavigate={props.onNavigate}
        initiallyOpen={props.initiallyOpen}
        logs={<pre data-testid="tab-logs">log line from the tab</pre>}
        device={<span data-testid="tab-device">Desktop</span>}
        control={<button type="button">Take control</button>}
      >
        <div data-testid="tab-slot">
          <div ref={screenRef} data-testid="screen" data-live-screen="" tabIndex={0} />
        </div>
        <input aria-label="tab search" />
        <TheaterButton />
      </TheaterHost>
    </div>
  )
}

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('opening the theater', () => {
  it('⤢ opens it on the same session: the same screen node, no second socket, no ticket read', async () => {
    const calls = stubFetch(NO_ACTIONS)
    render(<Tab />)
    const node = screen.getByTestId('screen')
    expect(sockets).toBe(1)

    fireEvent.click(screen.getByRole('button', { name: /theater/i }))
    await flush()

    const dialog = screen.getByRole('dialog', { name: /theater/i })
    expect(dialog.contains(node)).toBe(true)
    expect(screen.getByTestId('tab-slot').contains(node)).toBe(false)
    expect(sockets).toBe(1)
    for (const c of calls) {
      expect(c.url).not.toMatch(/live-session|ticket|\/live$/)
    }
  })

  it('`T` opens it on the same session too', async () => {
    stubFetch(NO_ACTIONS)
    render(<Tab />)
    const node = screen.getByTestId('screen')

    fireEvent.keyDown(document.body, { key: 't' })
    await flush()

    expect(screen.getByRole('dialog', { name: /theater/i }).contains(node)).toBe(true)
    expect(sockets).toBe(1)
  })

  it('`T` typed into the remote browser or a field is the person typing, not a shortcut', async () => {
    stubFetch(NO_ACTIONS)
    render(<Tab />)

    fireEvent.keyDown(screen.getByTestId('screen'), { key: 't' })
    fireEvent.keyDown(screen.getByLabelText('tab search'), { key: 'T' })
    fireEvent.keyDown(document.body, { key: 't', ctrlKey: true })
    await flush()

    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

describe('leaving the theater', () => {
  it('Esc returns to the tab at the same scroll, with the screen home and focus back on ⤢', async () => {
    stubFetch(NO_ACTIONS)
    render(<Tab />)
    const node = screen.getByTestId('screen')
    const pane = screen.getByTestId('pane')
    let top = 420
    Object.defineProperty(pane, 'scrollTop', {
      configurable: true,
      get: () => top,
      set: (v: number) => {
        top = v
      },
    })
    Object.defineProperty(pane, 'scrollHeight', { configurable: true, get: () => 5000 })
    Object.defineProperty(pane, 'clientHeight', { configurable: true, get: () => 600 })

    const button = screen.getByRole('button', { name: /theater/i })
    button.focus()
    fireEvent.click(button)
    await flush()
    expect(screen.getByRole('dialog')).toBeTruthy()

    // Something moved the page while the theater covered it.
    top = 0
    fireEvent.keyDown(document.body, { key: 'Escape' })
    await flush()

    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByTestId('tab-slot').contains(node)).toBe(true)
    expect(top).toBe(420)
    expect(document.activeElement).toBe(screen.getByRole('button', { name: /theater/i }))
    expect(sockets).toBe(1)
  })
})

describe('the left column', () => {
  it('switches between Timeline and Logs', async () => {
    stubFetch((url) =>
      url.includes('/live/actions')
        ? json({
            attempt_id: ATTEMPT,
            actions: [
              { at: '2026-10-11T20:00:00Z', kind: 'cdp', method: 'Page.navigate', url: 'https://example.com/login' },
              { at: '2026-10-11T20:00:05Z', kind: 'cdp', method: 'Input.insertText', length: 12 },
            ],
          })
        : json({ detail: 'Not Found' }, 404),
    )
    render(<Tab initiallyOpen />)
    await flush()

    const timeline = screen.getByRole('tab', { name: 'Timeline' })
    const logs = screen.getByRole('tab', { name: 'Logs' })
    // The panel shown is the one role queries find: a hidden panel is not.
    const shown = () => screen.getByRole('tabpanel')
    expect(timeline.getAttribute('aria-selected')).toBe('true')
    // LB-R2: the timeline says what the method did, not its protocol name.
    expect(await screen.findByText('The agent went to')).toBeTruthy()
    expect(shown().textContent).toContain('The agent went to')
    expect(shown().textContent).toContain('https://example.com/login')
    expect(shown().textContent).toContain('12 characters typed')
    expect(screen.queryByTestId('tab-logs')).toBeNull()

    fireEvent.click(logs)
    expect(logs.getAttribute('aria-selected')).toBe('true')
    expect(timeline.getAttribute('aria-selected')).toBe('false')
    expect(shown().contains(screen.getByTestId('tab-logs'))).toBe(true)
    expect(shown().textContent).not.toContain('The agent went to')

    fireEvent.click(timeline)
    expect(shown().textContent).toContain('The agent went to')
    expect(shown().textContent).not.toContain('log line from the tab')
  })

  it('becomes the hand-off checklist while the agent waits for a person, and Timeline is still a click away', async () => {
    stubFetch(NO_ACTIONS)
    function Waiting() {
      const ref = useRef<HTMLDivElement>(null)
      const session: TheaterSession = {
        taskId: TASK, tenantId: TENANT, attemptId: ATTEMPT, sessionId: 'sess_one', screen: null, live: true,
      }
      return (
        <TheaterHost
          session={session}
          initiallyOpen
          handoff={{ reason: 'Sign in to the fixture site', expect: 'login.example.com', deadline: new Date(Date.now() + 300_000).toISOString() }}
        >
          <div ref={ref} />
        </TheaterHost>
      )
    }
    render(<Waiting />)
    await flush()
    expect(screen.getByRole('tab', { name: 'Hand-off' }).getAttribute('aria-selected')).toBe('true')
    const panel = screen.getByRole('tabpanel')
    expect(panel.textContent).toContain('Sign in to the fixture site')
    expect(panel.textContent).toContain('login.example.com')
    expect(panel.textContent).toMatch(/\d+:\d\d left/)
    fireEvent.click(screen.getByRole('tab', { name: 'Timeline' }))
    expect(screen.getByRole('tabpanel').textContent).not.toContain('Sign in to the fixture site')
  })

  it('reads the timeline from the task route, never from a storage path', async () => {
    const calls = stubFetch(NO_ACTIONS)
    render(<Tab initiallyOpen />)
    await flush()
    const reads = calls.filter((c) => c.url.includes('/live/actions'))
    expect(reads.length).toBeGreaterThan(0)
    for (const c of reads) expect(c.url.startsWith(`/v1/tasks/${TASK}/live/actions`)).toBe(true)
  })
})

describe('prev and next', () => {
  const agents: LiveAgentRef[] = [
    { taskId: 'task_a', label: 'alpha', needsYou: false },
    { taskId: 'task_b', label: 'bravo', needsYou: true },
    { taskId: 'task_c', label: 'charlie', needsYou: false },
    { taskId: 'task_d', label: 'delta', needsYou: true },
  ]

  it('orders needs-you first and keeps the given order otherwise', () => {
    expect(orderLiveAgents(agents).map((a) => a.taskId)).toEqual(['task_b', 'task_d', 'task_a', 'task_c'])
  })

  it('walks the live agents in that order', async () => {
    stubFetch(NO_ACTIONS)
    const onNavigate = vi.fn()
    render(<Tab taskId="task_a" agents={agents} onNavigate={onNavigate} initiallyOpen />)
    await flush()

    fireEvent.click(screen.getByRole('button', { name: '‹ prev agent' }))
    expect(onNavigate).toHaveBeenLastCalledWith('task_d')
    fireEvent.click(screen.getByRole('button', { name: 'next ›' }))
    expect(onNavigate).toHaveBeenLastCalledWith('task_c')

    // OTHER LIVE lists the others in the same order; this agent is the
    // theater's own, named in its LIVE pill, not a row of the list.
    // Each row's name; its short task id (QA12-24) sits beside it.
    const items = within(screen.getByRole('region', { name: 'OTHER LIVE' }))
      .getAllByRole('listitem')
      .map((li) => li.querySelector('.th-agent-go, .th-agent-name')?.textContent ?? '')
    expect(items).toEqual(['bravo', 'delta', 'charlie'])
    expect(screen.getByTestId('theater-live-pill').textContent).toContain('alpha')
  })

  it('stops at the ends rather than wrapping', async () => {
    stubFetch(NO_ACTIONS)
    const onNavigate = vi.fn()
    render(<Tab taskId="task_b" agents={agents} onNavigate={onNavigate} initiallyOpen />)
    await flush()
    const prev = screen.getByRole('button', { name: '‹ prev agent' }) as HTMLButtonElement
    expect(prev.disabled).toBe(true)
    fireEvent.click(prev)
    expect(onNavigate).not.toHaveBeenCalled()
  })
})

describe('replay', () => {
  const own = (seq: number) => ({
    attempt_id: ATTEMPT,
    seq,
    object: `tenants/${TENANT}/tasks/${TASK}/attempts/${ATTEMPT}/live/rec-${String(seq).padStart(13, '0')}.live.webm`,
    started_at: `2026-10-11T20:0${seq}:00Z`,
  })
  const foreign = {
    attempt_id: ATTEMPT,
    seq: 7,
    object: `tenants/other/tasks/${TASK}/attempts/${ATTEMPT}/live/rec-7.live.webm`,
    started_at: '2026-10-11T20:07:00Z',
  }

  it('keeps only segments under the task tenant prefix with the recording suffix', () => {
    const listing: RecordingListing = {
      tenant_id: TENANT,
      task_id: TASK,
      segments: [
        own(1),
        foreign,
        { ...own(2), object: `tenants/${TENANT}/tasks/${TASK}/attempts/${ATTEMPT}/live/actions-2.live.jsonl` },
        { ...own(3), object: `tenants/${TENANT}/tasks/${TASK}/attempts/${ATTEMPT}/rec-3.live.webm` },
        { ...own(4), object: `tenants/${TENANT}/tasks/task_other/attempts/${ATTEMPT}/live/rec-4.live.webm` },
        { ...own(5), object: `tenants/${TENANT}/tasks/${TASK}/attempts/${ATTEMPT}/live/../../x/live/rec-5.live.webm` },
        own(6),
      ],
    }
    const session = { taskId: TASK, tenantId: TENANT }
    expect(segmentsOfTenant(listing, session).kept.map((s) => s.seq)).toEqual([1, 6])
    expect(segmentsOfTenant(listing, session).refused).toBe(5)
    // A listing answered for another tenant is refused whole.
    expect(segmentsOfTenant({ ...listing, tenant_id: 'other' }, session).kept).toEqual([])
    expect(segmentsOfTenant({ ...listing, task_id: 'task_other' }, session).kept).toEqual([])
  })

  it('reads the listing through the tenant-scoped task route and plays only the tenant segments', async () => {
    // The switch lives in sessionStorage (Q2); a localStorage choice is cleared, not honoured.
    sessionStorage.setItem(TENANT_PREF, TENANT)
    const calls = stubFetch((url) => {
      if (url.includes('/live/actions')) return json({ attempt_id: ATTEMPT, actions: [] })
      if (url.startsWith(`/v1/tasks/${TASK}/live/recordings`))
        return json({ tenant_id: TENANT, task_id: TASK, segments: [own(1), foreign, own(2)] })
      return json({ detail: 'Not Found' }, 404)
    })
    render(<Tab initiallyOpen />)
    await flush()

    fireEvent.click(screen.getByRole('button', { name: '◀◀ replay' }))
    await flush()

    const listingRead = calls.find((c) => c.url.startsWith(`/v1/tasks/${TASK}/live/recordings`))
    expect(listingRead?.tenant).toBe(TENANT)

    const video = (await screen.findByTestId('theater-replay')) as HTMLVideoElement
    const src = video.getAttribute('src') ?? ''
    expect(src.startsWith(`/v1/tasks/${TASK}/attempts/${ATTEMPT}/live/recordings/1?`)).toBe(true)
    expect(new URLSearchParams(src.split('?')[1]).get('tenant')).toBe(TENANT)

    const dialog = screen.getByRole('dialog')
    expect(dialog.innerHTML).not.toContain('tenants/other')
    expect(dialog.innerHTML).not.toContain('recordings/7')
    // The scrubber lays out exactly the two that are the tenant's.
    expect(screen.getByTestId('theater-scrubber').querySelectorAll('[data-segment]').length).toBe(2)

    // Leaving replay puts the live screen back in view.
    fireEvent.click(screen.getByRole('button', { name: 'live ●' }))
    expect(screen.queryByTestId('theater-replay')).toBeNull()
  })

  it('a refused listing plays nothing and says so', async () => {
    stubFetch((url) =>
      url.includes('/live/actions')
        ? json({ attempt_id: ATTEMPT, actions: [] })
        : json({ code: 'not_found', message: 'Not found.' }, 404),
    )
    render(<Tab initiallyOpen />)
    await flush()
    fireEvent.click(screen.getByRole('button', { name: '◀◀ replay' }))
    await flush()
    expect(screen.queryByTestId('theater-replay')).toBeNull()
    expect(screen.getByText(/no recording/i)).toBeTruthy()
  })
})
