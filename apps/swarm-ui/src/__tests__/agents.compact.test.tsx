// AGENTS V1, THE REMAINDER (agents.html, decided 2026-10-01).
//
// Beside an open agent the list is a column, and a column of ten-cell table
// rows is a column of truncations. The owner picked two-line rows as drawn:
// the mark, the name and the elapsed time; then the profile, the owner and the
// try -- or, on a waiting row, the reason it waits. Folded to the 64px strip
// the row is only its mark, and a mark alone does not say which agent it is,
// so hovering or focusing one draws a card beside it. On a phone the agent is
// a page of its own with a back link to the tab it came from.
//
// MUTATIONS, one per block: pass `compact={false}` from FlatRows; render the
// card without the strip check, or only on mouse; return 'Agents' from
// `backLabel` whatever the address.

import AGENTS_CSS from '../styles/agents.css?raw'
import STYLES from '../styles.css?raw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'

const api = vi.hoisted(() => ({ loadTasks: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { AgentsScreen } from '../Agents'
import { backLabel } from '../agentlist'
import { resetListSnap } from '../listSnap'
import { cascade } from './cssgate'

const NOW = '2026-09-23T12:00:00.000Z'
const THEN = '2026-09-23T10:00:00.000Z'

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'acme',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 5,
    created_at: THEN,
    updated_at: NOW,
    started_at: THEN,
    completed_at: ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(state) ? NOW : null,
    submitted_by: 'alex@acme.test',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: state === 'PARKED' ? 'QUOTA_EXHAUSTED' : null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: 3600,
    next_eligible_at: null,
    metadata: {},
    repository_ref: null,
    input: {},
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: null,
    ...over,
  }
}

const STEP = task('task_aaaaaaaa00000000000a', 'RUNNING', {
  workflow_id: 'wf_refactor-broker',
  step_id: 'fix-heartbeat',
  model: 'sonnet',
})
const LONE = task('task_bbbbbbbb00000000000b', 'RUNNING', { submitted_by: 'priya@acme.test' })

async function land(tasks: Task[], taskId: string | null): Promise<HTMLElement> {
  api.loadTasks.mockResolvedValue({
    status: 'ok',
    data: { tasks, tenant_id: 'acme' },
    fetchedAt: Date.now(),
  } satisfies Result<TaskPage>)
  const { container } = render(<AgentsScreen onOpen={() => {}} taskId={taskId} />)
  await waitFor(() => expect(container.querySelector('.ag-list-tabs [role="tab"]')).not.toBeNull())
  // THE RENDER THAT DREW THE LIST LANDED OUTSIDE `act` (waitFor polls with it
  // off), so its passive effects -- the « toggle's `useSyncExternalStore`
  // subscription among them -- may still be queued on React's scheduler. A
  // click before they run tells an empty listener set, and the toggle does
  // not re-render until the scheduler gets round to it: on a loaded CI runner,
  // after the assertion (#562, #567). Flush them before handing the screen over.
  await act(async () => {})
  return container as HTMLElement
}

function rowOf(container: HTMLElement, name: string): HTMLElement {
  const row = [...container.querySelectorAll<HTMLElement>('.row.is-compact')].find((r) =>
    r.querySelector('.cr-name')?.textContent?.includes(name),
  )
  expect(row, `no compact row named ${name}`).toBeDefined()
  return row!
}

afterEach(() => {
  delete document.documentElement.dataset.agentList
  localStorage.removeItem('swarm.agents.list')
  resetListSnap()
})

describe('beside an open agent, a row is two lines as drawn', () => {
  it('puts the mark, the name and the elapsed time on line one', async () => {
    const c = await land([STEP, LONE], LONE.id)
    const row = rowOf(c, 'fix-heartbeat')
    const cells = [...row.children].map((el) => el.className.split(' ')[0])
    // Line one, in the drawn order: the mark, the name, the elapsed time.
    expect(cells.slice(0, 3)).toEqual(['sk-st', 'agent', 'when'])
    expect(row.querySelector('.sk-st')?.textContent).toMatch(/running/i)
    expect(row.querySelector('.cr-name b')?.textContent).toBe('fix-heartbeat')
    expect(row.querySelector('.when')?.textContent).toMatch(/\d/)
    // A lone task is named by the id prefix, with the whole id in its title.
    const lone = rowOf(c, 'bbbbbbbb')
    expect(lone.querySelector('.cr-name .id')?.getAttribute('title')).toBe(LONE.id)
  })

  it('puts profile, owner and try on line two of a live row', async () => {
    const c = await land([STEP, LONE], LONE.id)
    const sub = rowOf(c, 'fix-heartbeat').querySelector('.cr-sub')
    expect(sub, 'no second line').not.toBeNull()
    const text = sub!.textContent ?? ''
    expect(text).toContain('claude-code · sonnet')
    expect(text).toContain('alex')
    expect(text).toContain('try 1/3')
    // The whole task id is printed under the name (#94), not on line two.
    expect(sub!.querySelector('.id')).toBeNull()
    expect(rowOf(c, 'fix-heartbeat').querySelector('.cr-name .tid-text')?.textContent).toBe(STEP.id)
  })

  it('gives a waiting row its reason in place of the try', async () => {
    const parked = task('task_cccccccc00000000000c', 'PARKED')
    const queued = task('task_dddddddd00000000000d', 'QUEUED')
    const c = await land([parked, queued], parked.id)
    const row = rowOf(c, 'cccccccc')
    expect(row.querySelector('.cr-sub')?.textContent).not.toMatch(/try/)
    expect(row.querySelector('.cr-sub')?.textContent).toContain('claude-code · alex')
    const why = row.querySelector('.why')
    expect(why, 'a waiting row lost the reason it waits').not.toBeNull()
    expect((why!.textContent ?? '').trim()).not.toBe('')
  })

  it('draws no column heads and no wide row beside an open agent', async () => {
    const open = await land([STEP, LONE], LONE.id)
    expect(open.querySelector('.row.is-head')).toBeNull()
    expect(open.querySelectorAll('.row.is-compact')).toHaveLength(2)
    expect(open.querySelector('.row.clickable:not(.is-compact)')).toBeNull()
    // Exactly the open agent says so, as on the wide list (AG-17).
    const current = open.querySelectorAll('.row[aria-current]')
    expect(current).toHaveLength(1)
    expect(current[0]!.querySelector('.id')?.getAttribute('title')).toBe(LONE.id)
  })

  // #503: "with no agent open, the list is a full-width ten-column table";
  // V1 is the compact list at every width.
  it('draws the same compact list when no agent is open', async () => {
    const c = await land([STEP, LONE], null)
    expect(c.querySelector('.row.is-head')).toBeNull()
    expect(c.querySelectorAll('.row.is-compact')).toHaveLength(2)
    expect(c.querySelector('.row.clickable:not(.is-compact)')).toBeNull()
    expect(c.querySelector('.row[aria-current]')).toBeNull()
  })

  it('puts the collapse toggle in the list header, only beside an open agent', async () => {
    const open = await land([STEP, LONE], LONE.id)
    const toggle = open.querySelector<HTMLButtonElement>('.ag-list-head .ag-collapse')
    expect(toggle, 'no « in the list header').not.toBeNull()
    expect(toggle!.getAttribute('aria-pressed')).toBe('false')
    expect(toggle!.textContent).toContain('«')
    // AWAITED, NOT READ AT ONCE: the toggle re-renders from the list-snap
    // store's notification, which reaches it only through its subscription.
    // Waiting for the state makes the assertion independent of when React
    // ran that effect; a toggle that never flips still fails, on the timeout.
    fireEvent.click(toggle!)
    await waitFor(() => expect(toggle!.getAttribute('aria-pressed')).toBe('true'))
    expect(toggle!.textContent).toContain('»')
    fireEvent.click(toggle!)
    await waitFor(() => expect(toggle!.getAttribute('aria-pressed')).toBe('false'))
    expect(toggle!.textContent).toContain('«')
    const closed = await land([STEP, LONE], null)
    expect(closed.querySelector('.ag-collapse')).toBeNull()
  })
})

describe('a strip row shows its agent on hover and on focus', () => {
  it('draws the card on hover, names the agent in it, and takes it down on leave', async () => {
    const c = await land([STEP, LONE], LONE.id)
    document.documentElement.dataset.agentList = 'strip'
    const row = rowOf(c, 'fix-heartbeat')
    expect(screen.queryByRole('tooltip')).toBeNull()

    fireEvent.mouseEnter(row)
    const card = screen.getByRole('tooltip')
    expect(card.textContent).toContain('fix-heartbeat')
    expect(card.textContent).toMatch(/running/i)
    expect(card.textContent).toContain('claude-code · sonnet')
    expect(card.textContent).toContain('alex')
    expect(card.querySelector('.ag-hovcard-when')?.textContent).toBe(row.querySelector('.when')?.textContent)
    expect(row.getAttribute('aria-describedby')).toBe(card.id)

    fireEvent.mouseLeave(row)
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(row.hasAttribute('aria-describedby')).toBe(false)
  })

  it('draws the card on keyboard focus too, and ↓ moves it to the next row', async () => {
    const c = await land([STEP, LONE], LONE.id)
    document.documentElement.dataset.agentList = 'strip'
    // THE LIST'S OWN ORDER, whatever it sorts by: ↓ goes to the next row in the
    // document, so the pair is read off the DOM rather than assumed.
    const [first, second] = [...c.querySelectorAll<HTMLElement>('.row.is-compact')]
    expect(first && second, 'the list drew fewer than two compact rows').toBeTruthy()
    const nameOf = (row: HTMLElement) => row.querySelector('.cr-name b')?.textContent ?? ''

    act(() => first!.focus())
    expect(screen.getByRole('tooltip').textContent).toContain(nameOf(first!))

    fireEvent.keyDown(first!, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(second)
    const cards = screen.getAllByRole('tooltip')
    expect(cards).toHaveLength(1)
    expect(cards[0]!.textContent).toContain(nameOf(second!))
    expect(cards[0]!.textContent).not.toContain(nameOf(first!))

    act(() => second!.blur())
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('draws no card when the list is the compact column, which already says it all', async () => {
    const c = await land([STEP, LONE], LONE.id)
    document.documentElement.dataset.agentList = 'list'
    const row = rowOf(c, 'fix-heartbeat')
    fireEvent.mouseEnter(row)
    act(() => row.focus())
    expect(screen.queryByRole('tooltip')).toBeNull()
  })
})

describe('the agent page leads back to the tab it came from', () => {
  it('names the tab in the list address', () => {
    expect(backLabel('work/running/waiting')).toBe('Waiting')
    expect(backLabel('work/running/live')).toBe('Live')
    expect(backLabel('work/running/recent/failed')).toBe('Recent')
  })

  it('says Agents when the address names no tab', () => {
    expect(backLabel('work/running')).toBe('Agents')
    expect(backLabel('work/running/livex')).toBe('Agents')
  })
})

// #503 AT 390x844: the section's pages sat as a boxed segmented control inside
// the page, with no sticky strip under the phone header. They are a strip of
// underlined tabs now, and on a phone it sticks. Asked of the cascade in the
// order the app loads the sheets (main.tsx imports App, and with it
// styles/agents.css, before styles.css). MUTATION: drop the phone rule.
describe('at 390 the section’s pages are a sticky strip', () => {
  it('draws the tabs as a strip, sticky on a phone and in the flow on a desktop', async () => {
    const c = await land([STEP, LONE], null)
    const strip = c.querySelector<HTMLElement>('.ag-list-tabs')!
    expect(strip.getAttribute('role')).toBe('tablist')
    expect(strip.closest('.ctl-seg, .c-seg'), 'the tabs are a boxed segmented control again').toBeNull()
    const sheets = AGENTS_CSS + '\n' + STYLES
    expect(cascade(sheets, strip, 'position', { width: 390 }).winner?.value).toBe('sticky')
    expect(cascade(sheets, strip, 'position', { width: 1440 }).winner?.value ?? 'static').toBe('static')
  })
})

// THE STRIP STICKS AT THE SCROLLER'S TOP EDGE, WHICH IS THE HEADER'S FOOT
// (#139). The phone header is a row of the frame above `.ctl-scroll`, not
// inside it, so a sticky `top` is measured from the header's foot already:
// `top: 44px` pinned the strip a header-height BELOW the header, rows showing
// through the gap, and its 36px twin moved it when the header compacted. This
// asked the old, wrong question (a strip and a header sharing one scroller)
// and passed. Asked of both header heights, in the frame's real nesting.
// MUTATION: set the phone rule's `top` back to 44px, or bring back the
// `.sk-app.is-scrolled` override.
describe('at 390 the strip sticks flush under the phone header, in either height', () => {
  it.each([
    ['at the top of the page', ''],
    ['once the page is scrolled', ' is-scrolled'],
  ])('sits at the scroller top %s', (_label, cls) => {
    const host = document.createElement('div')
    host.innerHTML =
      `<div class="sk-app${cls}"><header class="sk-pbar"></header>` +
      '<div class="sk-main"><div class="ctl-scroll"><div class="app"><main class="work">' +
      '<div role="tablist" class="ag-list-tabs"></div></main></div></div></div></div>'
    document.body.appendChild(host)
    try {
      const sheets = AGENTS_CSS + '\n' + STYLES
      const bar = host.querySelector('.sk-pbar')!
      const strip = host.querySelector('.ag-list-tabs')!
      expect(bar.closest('.ctl-scroll'), 'the header moved into the scroller; the strip would need its height').toBeNull()
      expect(cascade(sheets, strip, 'position', { width: 390 }).winner?.value).toBe('sticky')
      expect(cascade(sheets, strip, 'top', { width: 390 }).winner?.value, 'the strip sticks below a gap under the header').toBe('0')
    } finally {
      host.remove()
    }
  })
})
