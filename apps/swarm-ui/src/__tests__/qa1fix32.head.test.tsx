/**
 * LANE QA1-FIX-32 (part of #2596, the console visual QA pass 1 epic, and
 * #2391). The findings whose main file is agent-split/head.tsx or
 * WorkspacesYouAdmin.tsx, each pinned here against the fixture build of main.
 *
 *   overview-agents-20  the agent's head ignored its name: the Agents row read
 *                       `Fix flaky lease heartbeat test` (task_8ea5c26d's
 *                       `metadata.title`) and the opened head and the
 *                       breadcrumb read `claude-code agent · 8ea5c26d`.
 *   overview-agents-27  the head's meta line ended every wrapped line on a
 *                       separator (`account not read ·`).
 *   overview-agents-59  a READY task never admitted (generation 0) read
 *                       `attempt 1 of 3` in the head, `no attempt yet` in Now.
 *   admin-shell-10      the setup meter drew a step nothing measured as the
 *                       same solid grey bar as a step not done.
 */
import { render, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task as runTask } from './runfixture'
import type { Task } from '../types'

const api = vi.hoisted(() => ({
  loadResourceClasses: vi.fn(() => new Promise(() => {})),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgHead, AgHeadMeta } = await import('../agent-split/head')
const { useAgentName } = await import('../agentlist')

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const ID = 'task_8ea5c26d0123456789ab'

function row(over: Partial<Task> = {}): Task {
  return runTask({ id: ID, state: 'RUNNING', completed_at: null, ...over })
}

function head(task: Task, runs = task.attempt_count) {
  return render(
    <AgHead taskId={task.id} task={task} runs={runs} read="ok" reload={() => {}} onClose={() => {}} live={{ status: 'idle' }} reloadLive={() => {}} />,
  )
}

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

describe('overview-agents-20: the head names the agent as its Agents row does', () => {
  it('leads a lone agent with its title, in the head and the breadcrumb', () => {
    const { container } = head(row({ metadata: { title: 'Fix flaky lease heartbeat test' } }))
    // MUTATION: name the head with `agentName(task)` alone and it reads `claude-code agent · 8ea5c26d`.
    expect(container.querySelector('.ag-head-title')?.textContent).toBe('Fix flaky lease heartbeat test')
    // MUTATION: pass the task itself to `rememberAgentName` and the breadcrumb keeps the kind and id.
    expect(renderHook(() => useAgentName(ID)).result.current).toBe('Fix flaky lease heartbeat test')
  })

  it('names a step `<workflow title> · <step>`, as its row does (QA2 ovag2-14)', () => {
    const { container } = head(row({ id: 'task_step0123456789abcd', step_id: 'review', workflow_id: 'wf_0123456789abcdef0123', metadata: { title: 'Release notes' } }))
    // MUTATION: name a step by `ownTitle` alone and the head, the breadcrumb
    // and the document title read `review` while the row reads `Release notes · review`.
    expect(container.querySelector('.ag-head-title')?.textContent).toBe('Release notes · review')
    expect(renderHook(() => useAgentName('task_step0123456789abcd')).result.current).toBe('Release notes · review')
  })

  it('keeps a step’s name alone when its workflow has no title, as its row does', () => {
    const { container } = head(row({ id: 'task_step1123456789abcd', step_id: 'review', workflow_id: 'wf_0123456789abcdef0123', metadata: { title: '  ' } }))
    expect(container.querySelector('.ag-head-title')?.textContent).toBe('review')
    expect(renderHook(() => useAgentName('task_step1123456789abcd')).result.current).toBe('review')
  })

  it('keeps `agentName`’s words for an agent with no title', () => {
    const { container } = head(row({ id: 'task_a073aff50123456789ab' }))
    expect(container.querySelector('.ag-head-title')?.textContent).toBe('claude-code agent · a073aff5')
  })
})

describe('overview-agents-59: a task never admitted has no attempt in the head', () => {
  it('says `no attempt yet` for READY at generation 0, as the Now card does', () => {
    const { container } = head(row({ state: 'READY', attempt_count: 1, current_generation: 0, current_lease_id: null }), 1)
    const hl = container.querySelector('.ag-head-hl')!.textContent ?? ''
    // MUTATION: drop the generation check and the head reads `attempt 1 of 3` over `no attempt yet`.
    expect(hl).toContain('no attempt yet')
    expect(hl).not.toMatch(/attempt 1 of/)
  })

  it('still counts the attempt of a task that ran', () => {
    const { container } = head(row({ state: 'READY', attempt_count: 1, current_generation: 1, current_lease_id: null }), 1)
    expect(container.querySelector('.ag-head-hl')!.textContent).toContain('attempt 1 of 3')
  })
})

describe('overview-agents-27: a separator only between two facts, never ending a line', () => {
  it('opens every fact but the first with a dot hung in the gap, which the list clips at a line’s start', () => {
    const t = row({
      account: { status: 'unread', account_id: null, provider: null, attempt_id: null, generation: null, swapped_from: null, swaps: [] },
      workflow_id: 'wf_0123456789abcdef0123',
    } as Partial<Task>)
    const { container } = render(<AgHeadMeta task={t} />)
    const facts = [...container.querySelectorAll<HTMLElement>('.ag-head-facts > li:not(.is-nodot)')]
    expect(facts.length).toBeGreaterThanOrEqual(4)
    for (const env of [WIDE, PHONE]) {
      facts.forEach((li, i) => {
        // MUTATION: put the dot back on `::after` and every wrapped line ends on one (`account not read ·`).
        expect(painted(li, 'content', env, 'after') ?? 'none', `fact ${i} trails a separator`).toBe('none')
        const before = painted(li, 'content', env, 'before') ?? 'none'
        expect(before, `fact ${i}`).toBe(i === 0 ? 'none' : '"·"')
        if (i > 0) {
          // The dot is the 8px column gap, and its margins take back the gap and the fact's 5px.
          expect(painted(li, 'width', env, 'before')).toBe('8px')
          expect(painted(li, 'margin-inline', env, 'before')).toBe('-8px -5px')
        }
      })
      const ul = container.querySelector('.ag-head-facts')!
      // MUTATION: drop the clip and a fact that opens a line shows its dot before it (`· workflow …`).
      expect(painted(ul, 'overflow-x', env)).toBe('clip')
      expect(painted(ul, 'column-gap', env) ?? (painted(ul, 'gap', env) ?? '').split(/\s+/)[1]).toBe('8px')
      expect(painted(container.querySelector('.ag-head-facts > li')!, 'gap', env)).toBe('5px')
    }
    // The dispatch chips are not a fact: no dot opens them.
    const dispatch = container.querySelector<HTMLElement>('.ag-head-facts > li.ag-head-dispatch')!
    expect(dispatch.hasAttribute('data-sep')).toBe(false)
  })
})

describe('admin-shell-10: a step nothing measured is drawn apart from a step not done', () => {
  it('draws an unknown segment hollow and dashed, a todo one solid', () => {
    const host = document.createElement('div')
    host.innerHTML = '<div class="cfg-progress"><div class="cfg-meter-bar"><i class="is-done"></i><i class="is-todo"></i><i class="is-unknown"></i></div></div>'
    document.body.appendChild(host)
    hosts.push(host)
    const [, todo, unknown] = [...host.querySelectorAll('i')]
    expect(painted(todo!, 'background', WIDE)).toBe('var(--line)')
    // MUTATION: drop `.cfg-meter-bar > i.is-unknown` and the unmeasured step is the todo step's grey bar.
    expect(painted(unknown!, 'background', WIDE)).toBe('none')
    expect(painted(unknown!, 'border', WIDE)).toBe('1px dashed var(--ctl-absent)')
    expect(painted(unknown!, 'box-sizing', WIDE)).toBe('border-box')
  })
})

describe('CIFIX-2616: the head reads the title without importing the Agents screen', () => {
  it('names the agent as its Agents row does, for every shape of title', async () => {
    const head = await import('../agent-split/head')
    const { ownTitle: rowTitle } = await import('../Agents')
    const shapes: Pick<Task, 'step_id' | 'metadata'>[] = [
      { step_id: null, metadata: { title: '  Fix flaky lease heartbeat test ' } },
      { step_id: 'implement', metadata: { title: 'Fix flaky lease heartbeat test' } },
      { step_id: null, metadata: { title: '   ' } },
      { step_id: null, metadata: { title: 42 } },
      { step_id: null, metadata: {} },
      { step_id: null, metadata: null },
    ]
    for (const s of shapes) expect(head.ownTitle(s)).toBe(rowTitle(s))
  })

  it('does not import ../Agents, which the drawer tests replace with a mock of AgentsScreen alone', async () => {
    const { readFileSync } = await import('node:fs')
    const { join } = await import('node:path')
    const src = readFileSync(join(__dirname, '..', 'agent-split', 'head.tsx'), 'utf8')
    // MUTATION: import `ownTitle` from '../Agents' again and the 50 drawer tests in artifacts.pane, artifacts.rawpatch and decision.card throw on a missing mock export.
    expect(src).not.toMatch(/from '\.\.\/Agents'/)
  })
})
