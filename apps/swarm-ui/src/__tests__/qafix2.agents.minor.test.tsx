// VISUAL QA RUN 2, THE AGENT LIST AND THE PANE STRIP'S MINOR FINDINGS
// (lane QAFIX2-AGENTS-MINOR, part of #1680; run wf_b6e2441b419f423a84eb, the
// fixture build of main at 75148351, 1440 and 390, light and dark).
//
//   task-012  the open agent's row in the list is marked: the selection
//             wash styles.css draws on `.row[aria-current]`, which stopped
//             reaching the row when QA-032 moved `aria-current` onto the
//             row's opener;
//   task-016  at 390 the agent split's one-row strip sits on ONE baseline,
//             the strip's own: the inactive tabs no longer draw the per-tab
//             hairline a WRAPPING strip needs;
//   task-017  a task id is one text node, so a screen reader reads it as one
//             word and a plain copy carries no break; the cut that keeps its
//             distinguishing end is the line's own;
//   work-a-007 a live row's try leads its meta line, so the 390 ellipsis
//             cuts the profile and never `of 3`.
//
// task-010 is not here: it is the fixture build's data (its only agent is a
// `claude-code` one and its session read says `live_browser: false`), not the
// console -- the PR body says so.
//
// Layout and colour through `painted` (marks.ts), as access.page.test.tsx
// does: jsdom applies no `@media` block.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Task, TaskPage, TaskState } from '../types'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadAttempts: vi.fn(async () => ({ status: 'error', error: { kind: 'unreachable', httpStatus: null, code: null, message: 'no' } })),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { AgentsScreen } from '../Agents'
import { TaskIdLine } from '../TaskIdLine'
import { resetListSnap } from '../listSnap'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390, height: 844 }

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
    park_reason: null,
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

async function land(tasks: Task[], taskId: string | null): Promise<HTMLElement> {
  api.loadTasks.mockResolvedValue({
    status: 'ok',
    data: { tasks, tenant_id: 'acme' },
    fetchedAt: Date.now(),
  } satisfies Result<TaskPage>)
  const { container } = render(<AgentsScreen onOpen={() => {}} taskId={taskId} />)
  await waitFor(() => expect(container.querySelector('.row.is-compact')).not.toBeNull())
  await act(async () => {})
  return container as HTMLElement
}

const rowOf = (c: HTMLElement, id: string) => c.querySelector<HTMLElement>(`.row.is-compact[data-task-id="${id}"]`)!

function tree(html: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = html
  document.body.appendChild(host)
  return host
}

afterEach(() => {
  document.body.innerHTML = ''
  resetListSnap()
})

describe('task-012: the open agent’s row is marked, to the eye and to a screen reader', () => {
  const OPEN = 'task_a073aff5'
  const OTHER = 'task_8ea5c26d'

  it('draws the selection on the open row and on no other', async () => {
    const c = await land([task(OTHER, 'RUNNING'), task(OPEN, 'RUNNING')], OPEN)
    // The opener says so, found by its role and its current state.
    const opener = screen.getByRole('button', { current: true })
    // MUTATION: drop `aria-current` from the opener in CompactRow.
    expect(opener.closest('.row')?.getAttribute('data-task-id')).toBe(OPEN)
    for (const env of [WIDE, PHONE]) {
      // MUTATION: drop `aria-current` from the row div in CompactRow (QA-032's
      // move): styles.css's `.row[aria-current]` wash matches nothing.
      expect(painted(rowOf(c, OPEN), 'box-shadow', env) ?? 'none', `open row at ${env.width}`).toMatch(/inset 2px 0 0/)
      // MUTATION: mark every row `aria-current`.
      expect(painted(rowOf(c, OTHER), 'box-shadow', env) ?? '', `closed row at ${env.width}`).not.toMatch(/inset 2px 0 0/)
    }
  })

  it('marks no row when no agent is open', async () => {
    const c = await land([task(OTHER, 'RUNNING'), task(OPEN, 'RUNNING')], null)
    // MUTATION: set `aria-current` on the row whether or not it is open.
    expect(c.querySelector('.row[aria-current]')).toBeNull()
    expect(screen.queryByRole('button', { current: true })).toBeNull()
  })
})

describe('task-016: at 390 the agent split’s strip sits on one baseline', () => {
  const STRIP =
    '<div class="ag-split"><div class="ag-tabs-edge"><div class="c-tabs ag-split-tabs" role="tablist">' +
    '<button role="tab" aria-selected="true">Details</button><button role="tab" aria-selected="false">Logs</button>' +
    '<button role="tab" aria-selected="false">Changes</button></div></div></div>'

  it('draws no hairline of its own under an inactive tab, and the strip’s rule under them all', () => {
    const host = tree(STRIP)
    const strip = host.querySelector<HTMLElement>('.ag-split-tabs')!
    for (const tab of within(strip).getAllByRole('tab', { selected: false })) {
      // MUTATION: drop the split strip from the phone rule's exemption: each
      // inactive tab is a half-box on its own inset hairline again.
      expect(painted(tab, 'box-shadow', PHONE) ?? 'none', tab.textContent ?? '').toBe('none')
    }
    // MUTATION: drop `border-bottom` from `.c-tabs`: no baseline at all.
    expect(painted(strip, ['border-bottom', 'border-bottom-width'], PHONE) ?? '').toMatch(/1px solid/)
  })

  it('leaves a wrapping strip elsewhere its per-tab hairline (V119)', () => {
    const host = tree('<nav class="c-tabs"><a>Overview</a><a aria-current="page">Graph</a></nav>')
    // MUTATION: exempt every `.c-tabs` rather than the split's one-row strip.
    for (const tab of [...host.querySelectorAll('a')]) {
      expect(painted(tab, 'box-shadow', PHONE)).toBe('inset 0 -1px 0 var(--line)')
    }
  })
})

describe('task-017: a task id is one text node', () => {
  const ID = 'task_a073aff5'

  it('reads as one word and copies as one line', () => {
    const { container } = render(<TaskIdLine id={ID} />)
    const text = container.querySelector<HTMLElement>('.tid-text')!
    // MUTATION: split the id into `.tid-head` and `.tid-tail` spans again.
    expect([...text.childNodes].map((n) => n.nodeType)).toEqual([Node.TEXT_NODE])
    expect(text.textContent).toBe(ID)
    // The copy button names the whole id too.
    expect(screen.getByRole('button', { name: `Copy task id ${ID}` })).toBeTruthy()
  })

  it('is one line at 390, cut at its start so the distinguishing end stays', () => {
    const { container } = render(<TaskIdLine id={ID} />)
    const text = container.querySelector<HTMLElement>('.tid-text')!
    // MUTATION: `display: flex` back on `.tid-text`: its children are blocks
    // and a selection across them copies a break.
    expect(painted(text, 'display', PHONE) ?? 'inline').not.toMatch(/flex|grid/)
    // MUTATION: drop `white-space: nowrap` / `overflow: hidden` /
    // `text-overflow: ellipsis` from `.tid-text`.
    expect(painted(text, 'white-space', PHONE)).toBe('nowrap')
    expect(painted(text, 'overflow', PHONE)).toBe('hidden')
    expect(painted(text, 'text-overflow', PHONE)).toBe('ellipsis')
    // MUTATION: drop `direction: rtl`: the ellipsis eats the end that tells
    // two ids apart, keeping the `task_` every id shares.
    expect(painted(text, 'direction', PHONE)).toBe('rtl')
    // MUTATION: drop `text-align: left`: a short id is pushed to the right.
    expect(painted(text, 'text-align', PHONE)).toBe('left')
  })
})

describe('work-a-007: a live row’s try leads its meta line, so the cut never takes it', () => {
  it('prints the state word and the try before the profile and the owner', async () => {
    const id = 'task_19d91b82'
    const c = await land([task(id, 'DISPATCHED', { model: 'claude-opus-5-5' })], null)
    const sub = rowOf(c, id).querySelector<HTMLElement>('.cr-sub')!
    await waitFor(() => expect(sub.textContent).toContain('attempt 1 of 3'))
    const said = sub.textContent ?? ''
    // MUTATION: put `.cr-try` back after the profile and owner: the 390
    // ellipsis cuts `attempt … of 3` after `attempt`.
    expect(said.indexOf('of 3'), said).toBeLessThan(said.indexOf('claude-code'))
    expect(said.indexOf('dispatched'), said).toBeLessThan(said.indexOf('claude-code'))
    // MUTATION: drop the separator after the try: `attempt 1 of 3claude-code`.
    expect(said).toMatch(/attempt 1 of 3 · claude-code · claude-opus-5-5 · alex/)
    // A fact longer than the row is still cut at its end (agents.css `.cr-sub`);
    // at 390 the line wraps between its facts (qafix5.agents, work-a-007).
    expect(painted(sub, 'text-overflow', PHONE)).toBe('ellipsis')
  })

  it('keeps a finished row’s line as it was: profile, owner, then the run', async () => {
    const id = 'task_a3881bec'
    const c = await land([task(id, 'SUCCEEDED')], null)
    const sub = rowOf(c, id).querySelector<HTMLElement>('.cr-sub')!
    // MUTATION: lead every row with the try, so a finished row prints one.
    expect(sub.textContent).toMatch(/^claude-code · alex · ran /)
  })
})
