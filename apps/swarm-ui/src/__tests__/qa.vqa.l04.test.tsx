/**
 * VISUAL QA LANE L04 (#1038): the Agents list, the split and the phone drawer.
 *
 *   V058  the split's metric strip cut `Peak memory`, wrapped Cost's sub-line
 *         to eight lines and held ~220px of a 600px pane.
 *   V059  opening Changes folded the list and squeezed the provenance line
 *         into the 64px strip, with no way back named on screen.
 *   V060  Waiting printed `Eligible again 2026-10-11T02:50:58.413Z`.
 *   V061  the profile select grew to its longest option; a failed row printed
 *         its whole multi-line error.
 *   V146  a failed row's reason was amber beside its red mark and rule.
 *   V147  `26 loaded` was stranded on a line of its own; « drew as `« [`.
 *   V148  the phone drawer's tabs wrapped to two rows; a grip sliver showed.
 *   V149  `account not read` was bold mono in a sans line (this lane's half;
 *         the phone header's tenant is L14's, in Shell.tsx).
 *   V151  an empty Agents read dropped the tabs, the search and the filters.
 *   V152  Escape did not close the split a mouse click had opened.
 *
 * MUTATIONS, one per case: put the ISO back in `whyAgent`; let
 * `loadAgentsPage` answer `empty`; put `.ag-scope` back as the toolbar's last
 * item, or the `<kbd>` back in «; drop the select cap or the row clamp; drop
 * `is-bad`; put the phone tabs' wrap back or drop the divider rule; drop the
 * strip's count-note or back-link rule; put `nowrap` back on `.dt-sc-v` or
 * drop the sub-line clamp; draw `not read` mono; drop the window Escape
 * listener. Each turns its case red.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Task, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted, resolveColour } from './marks'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadTasks: vi.fn(),
  loadResourceClasses: vi.fn(),
  loadChildren: vi.fn(),
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
  loadCheckpoints: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentSplit, AgHeadMeta } = await import('../AgentSplit')
const { AgentsScreen } = await import('../Agents')
const { resetListSnap } = await import('../listSnap')
const { eligibleAgain, whyAgent } = await import('../types')

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 400, height: 800 }
const ID = 'task_0123456789abcdef0123'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

const hosts: HTMLElement[] = []
function tree(html: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = html
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] } satisfies TaskPage))
  api.loadCheckpoints.mockReturnValue(new Promise(() => {}))
  api.loadResourceClasses.mockResolvedValue(ok({ resource_classes: {} }))
})

afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
  delete document.documentElement.dataset.agentList
})

function row(id: string, over: Partial<Task> = {}): Task {
  return runTask({ id, state: 'RUNNING', completed_at: null, ...over })
}

async function list(tasks: Task[], taskId: string | null = null): Promise<HTMLElement> {
  api.loadTasks.mockResolvedValue(ok({ tasks, tenant_id: 'acme' } satisfies TaskPage))
  const { container } = render(<AgentsScreen onOpen={() => {}} taskId={taskId} />)
  await waitFor(() => expect(container.querySelector('.ag-list-tabs [role="tab"]')).not.toBeNull())
  await act(async () => {})
  return container as HTMLElement
}

describe('V060: a parked row says when it is eligible in words, not ISO', () => {
  it('prints a local clock time and the wait left, never the raw instant', () => {
    const now = Date.parse('2026-10-11T02:40:00.000Z')
    const iso = '2026-10-11T02:50:58.413Z'
    const said = eligibleAgain(iso, now)!
    expect(said).not.toContain(iso)
    expect(said).not.toMatch(/\d{4}-\d{2}-\d{2}T/)
    expect(said).toMatch(/^Eligible again at \d{2}:\d{2}:\d{2} \(in 10m\)\.$/)
    expect(eligibleAgain('2026-10-11T02:30:00.000Z', now)).toMatch(/^Eligible again since \d{2}:\d{2}/)
    expect(eligibleAgain(null, now)).toBeNull()
    const why = whyAgent(row(ID, { state: 'PARKED', park_reason: 'QUOTA_EXHAUSTED', next_eligible_at: iso }))
    expect(why).not.toContain(iso)
    expect(why).toMatch(/Eligible again/)
  })
})

describe('V151: an empty read keeps the tabs, the search and the filters', () => {
  it('draws the list chrome and the tab’s own real zero', async () => {
    api.loadTasks.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    const { container } = render(<AgentsScreen onOpen={() => {}} />)
    await waitFor(() => expect(container.querySelector('.ag-list-tabs')).not.toBeNull())
    expect(screen.getAllByRole('tab').map((t) => t.textContent?.replace(/\s*\d+$/, ''))).toEqual(['Live', 'Waiting', 'Recent'])
    expect(screen.getByRole('searchbox', { name: 'Find by name or task id' })).toBeTruthy()
    expect(container.querySelector('.ag-filter select')).not.toBeNull()
    expect(container.querySelector('.ctl-empty .ctl-mark.is-zero')).not.toBeNull()
  })
})

describe('V147: the toolbar strands nothing and « is one glyph', () => {
  it('puts the scope at the search box’s end, and draws « without the `[` key box', async () => {
    const c = await list([row('task_aaaaaaaa00000000000a')], 'task_aaaaaaaa00000000000a')
    const scope = c.querySelector('.ag-scope')!
    expect(scope.parentElement?.classList.contains('ag-find')).toBe(true)
    expect(scope.textContent).toBe('1 loaded')
    const fold = c.querySelector<HTMLButtonElement>('.ag-collapse')!
    expect(fold.textContent?.trim()).toBe('«')
    expect(fold.querySelector('kbd')).toBeNull()
    expect(fold.getAttribute('aria-label')).toContain('[')
  })
})

describe('V061 and V146: the profile select is capped; a failed row’s reason is clamped and red', () => {
  it('caps the select at every width and titles it with its value', async () => {
    const long = 'a-very-long-runner-profile-name-that-should-never-size-the-toolbar'
    const c = await list([row('task_aaaaaaaa00000000000a', { runner_profile: long })])
    const select = c.querySelector<HTMLSelectElement>('.ag-list-head .ag-filter select')!
    expect(painted(select, 'max-width', WIDE)).toBe('16em')
    expect(painted(select, 'max-width', PHONE)).toBe('16em')
    expect(select.getAttribute('title')).toBe('all')
  })

  it('clamps a failed row’s error to two lines, whole in its title, in --bad-ink', async () => {
    const error = 'Traceback (most recent call last):\n  File "x.py", line 1\nRuntimeError: the worker exited 137\nmore\nlines'
    const c = await list([row('task_ffffffff00000000000f', { state: 'FAILED', last_error: error, completed_at: '2026-10-11T02:00:00Z' })])
    const recent = screen.getAllByRole('tab').find((t) => t.textContent?.startsWith('Recent'))!
    fireEvent.click(recent)
    const why = await waitFor(() => {
      const w = c.querySelector<HTMLElement>('.row.is-compact > .why')
      expect(w).not.toBeNull()
      return w!
    })
    expect(why.getAttribute('title')).toBe(error)
    expect(why.classList.contains('is-bad')).toBe(true)
    for (const env of [WIDE, PHONE]) {
      expect(painted(why, ['-webkit-line-clamp', 'line-clamp'], env)).toBe('2')
      expect(painted(why, 'overflow', env)).toBe('hidden')
    }
    for (const theme of ['dark', 'light'] as const) {
      expect(resolveColour(painted(why, 'color', { width: 1440, theme })!, theme)).toEqual(resolveColour('var(--bad-ink)', theme))
    }
  })
})

describe('V059: the folded list draws only its marks, and the way back moves into the agent', () => {
  it('hides the count note in the strip and shows the agent’s back link there', () => {
    const host = tree(
      '<div class="app has-inspector"><main class="work"><div><p class="c-count-note">26 loaded · 3 live · acme</p></div></main>' +
        '<div class="drawer ctl-drawer ag-split on-changes"><button class="ctl-agent-back">‹ Agents</button></div></div>',
    )
    const note = host.querySelector('.c-count-note')!
    const back = host.querySelector('.ctl-agent-back')!
    document.documentElement.dataset.agentList = 'strip'
    expect(painted(note, 'position', WIDE)).toBe('absolute')
    expect(painted(note, 'clip-path', WIDE)).toBe('inset(50%)')
    expect(painted(back, 'display', WIDE)).toBe('inline-flex')
    document.documentElement.dataset.agentList = 'list'
    expect(painted(note, 'position', WIDE) ?? 'static').toBe('static')
    expect(painted(back, 'display', WIDE)).toBe('none')
  })
})

describe('V148: the phone drawer’s tabs are one row, and no grip shows', () => {
  it('hides the divider wherever the agent covers the list, and keeps it beside the list', () => {
    const host = tree('<div class="drawer ctl-drawer ag-split"><div class="ctl-inspector-grip ag-divider"></div></div>')
    const grip = host.querySelector('.ag-divider')!
    expect(painted(grip, 'display', PHONE)).toBe('none')
    expect(painted(grip, 'display', { width: 1000 })).toBe('none')
    expect(painted(grip, 'display', WIDE) ?? 'block').not.toBe('none')
  })

  it('packs the tabs into one scrolling row on a phone', () => {
    const host = tree('<div class="ag-split"><div class="ag-tabs-edge"><div class="c-tabs ag-split-tabs" role="tablist"><button role="tab">Details</button></div></div></div>')
    const strip = host.querySelector('.ag-split-tabs')!
    expect(painted(strip, 'flex-wrap', PHONE)).toBe('nowrap')
    expect(painted(strip, ['overflow-x', 'overflow'], PHONE)).toBe('auto')
    expect(painted(host.querySelector('[role="tab"]')!, ['padding-inline', 'padding-left'], PHONE)).toBe('8px')
  })
})

describe('V058: the metric strip cuts no figure and stays short in a narrow pane', () => {
  it('lets a figure’s unit wrap rather than ellipsising it, and clamps sub-lines in a 480-639px pane', () => {
    const host = tree(
      '<div class="ag-split"><div class="ag-split-pane"><div class="dt"><div class="dt-strip"><div class="dt-sc">' +
        '<span class="dt-sc-l">Peak memory</span><span class="dt-sc-v">26.7 MiB<small> 13%</small></span><span class="dt-sc-s">of 8 GiB</span>' +
        '</div></div></div></div></div>',
    )
    const v = host.querySelector('.dt-sc-v')!
    const sub = host.querySelector('.dt-sc-s')!
    for (const container of [600, 900]) {
      expect(painted(v, 'white-space', { width: 1440, container })).not.toBe('nowrap')
      expect(painted(v, 'text-overflow', { width: 1440, container }) ?? 'clip').not.toBe('ellipsis')
    }
    expect(painted(host.querySelector('.dt-sc-v > small')!, 'white-space', WIDE)).toBe('nowrap')
    expect(painted(sub, ['-webkit-line-clamp', 'line-clamp'], { width: 1440, container: 600 })).toBe('2')
    expect(painted(host.querySelector('.dt-sc')!, 'padding', { width: 1440, container: 600 })).toBe('8px 8px 9px')
  })
})

describe('V149: an account that was not read is words, not an id', () => {
  it('draws `not read` in the line’s own face and a real account id in mono', () => {
    const { container, rerender } = render(<AgHeadMeta task={row(ID, { account: null })} />)
    const li = () => [...container.querySelectorAll('.ag-head-facts > li')].find((l) => l.textContent?.startsWith('account'))!
    expect(li().textContent).toBe('account not read')
    expect(li().querySelector('.mono')).toBeNull()
    expect(li().querySelector('b')).toBeNull()
    rerender(
      <AgHeadMeta
        task={row(ID, {
          account: { status: 'assigned', account_id: 'claude-max-2', provider: 'anthropic', attempt_id: 'att_1', generation: 1, swapped_from: null, swaps: [] },
        })}
      />,
    )
    expect(li().querySelector('b.mono')?.textContent).toBe('claude-max-2')
  })
})

describe('V152: Escape closes the split wherever focus is', () => {
  it('closes after a mouse click left focus on the row, and leaves a text field’s Escape alone', async () => {
    api.loadTask.mockResolvedValue(ok(row(ID, { state: 'SUCCEEDED', completed_at: '2026-10-11T02:00:00Z' })))
    const go = vi.fn()
    const rowEl = tree(`<div class="row clickable is-compact" tabindex="0" data-task-id="${ID}"></div><input type="search" />`)
    render(
      <div className="app has-inspector">
        <AgentSplit taskId={ID} pane="detail" artifact={null} closeTo="work/running/recent" go={go} base={`work/task/${ID}`} />
      </div>,
    )
    await waitFor(() => expect(document.querySelector('.ag-split')).not.toBeNull())
    go.mockClear()
    const field = rowEl.querySelector('input')!
    field.focus()
    fireEvent.keyDown(field, { key: 'Escape' })
    expect(go).not.toHaveBeenCalled()
    const r = rowEl.querySelector<HTMLElement>('.row')!
    r.focus()
    fireEvent.keyDown(r, { key: 'Escape' })
    expect(go).toHaveBeenCalledWith('work/running/recent')
  })
})
