// #66'S OWN REPRO, ON THE AGENTS LIST AND THE AGENT INSPECTOR.
//
// THE DEFECT. `blockers.belowunits.test.tsx` proved Capacity, Profiles and
// Submit say a browser task (2 units) can never be admitted under
// `resource:browser` at `hard_limit 1`, and that a person has to raise it --
// but that fix was a PARALLEL `blockerVerdict`/`verdictCopy` in Blockers.tsx,
// threaded only into those three screens. Work > Agents (`Agents.tsx`) and
// the agent inspector (`AgentDetail.tsx`) go through `whyAgent`/
// `whyNeedsAction` in types.ts instead, which still called the untouched
// `blockerCeiling`/`ceilingCopy` -- so #66's own repro still read "This
// resource class is busy platform-wide. (0/1)" on both, and was never
// flagged as needing a person.
//
// THE FIX. `blockerCeiling`/`ceilingCopy` (types.ts) now take the task's
// `units` themselves, read from `GET /v1/resource-classes` -- never the
// bundled `RESOURCE_UNITS` table -- so `whyAgent`/`whyNeedsAction` carry the
// same verdict Blockers.tsx already gave Capacity/Profiles/Submit.
//
// MUTATION: stop threading `units` into `whyAgent`/`whyNeedsAction` from
// either screen (drop the catalogue read, or drop the argument) and both
// tests below read "busy platform-wide. (0/1)" again, filed as a routine
// wait rather than one that needs a person.

import { describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

import type { AgentRun, ResourceClasses } from '../api'
import type { Task } from '../types'
import { attempt, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadResourceClasses: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentsScreen } = await import('../Agents')
const { Run } = await import('../AgentDetail')

// ---------------------------------------------------------------------------

const CLASSES: ResourceClasses = {
  standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 4, units: 1 },
  browser: { name: 'browser', cpu: 8, memory_gib: 16, disk_gib: 8, units: 2 },
  large: { name: 'large', cpu: 8, memory_gib: 32, disk_gib: 16, units: 4 },
}

const NEVER = /can never (be )?admit/i
const RAISE = /(somebody|someone|a person) (has|needs) to raise/i

/** #66's repro: a browser task (2 units) under `resource:browser` at limit 1. */
function browserTask(over: Partial<Task> = {}): Task {
  return task({
    id: 'tsk_browser0',
    state: 'READY',
    resource_class: 'browser',
    started_at: null,
    completed_at: null,
    blocked_by: [{ pool: 'resource:browser', reason: 'RESOURCE_CLASS_LIMIT', limit: 1, active: 0 }],
    ...over,
  })
}

function ok<T>(data: T) {
  return { status: 'ok' as const, data, fetchedAt: Date.now() }
}

// ---------------------------------------------------------------------------
// Work > Agents
// ---------------------------------------------------------------------------

describe('the Agents list, a browser task under resource:browser at limit 1', () => {
  it('says the task can never be admitted and a person has to raise it, not "busy platform-wide. (0/1)"', async () => {
    api.loadTasks.mockResolvedValue(ok({ tasks: [browserTask()], tenant_id: 'acme' }))
    api.loadResourceClasses.mockResolvedValue(ok({ resource_classes: CLASSES }))

    render(<AgentsScreen onOpen={() => {}} />)

    const why = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.row .why')
      expect(el, 'no row drew a why line').not.toBeNull()
      return el!
    })
    expect(why.textContent ?? '').toMatch(NEVER)
    expect(why.textContent ?? '').toMatch(RAISE)
    expect(why.textContent ?? '', 'not read as busy: nothing is running').not.toMatch(/busy platform-wide/i)
    expect(why.textContent ?? '').not.toMatch(/\(0\/1\)/)
    // AG-14: a line that can never be admitted needs a person, and is --warn.
    expect(why.classList.contains('is-warn'), 'can-never-be-admitted needs a person').toBe(true)
  })

  /** The control: a limit the task fits under is still read as busy. */
  it('keeps "busy" for the same pool at a ceiling the task fits under', async () => {
    api.loadTasks.mockResolvedValue(
      ok({
        tasks: [
          browserTask({
            id: 'tsk_browser1',
            blocked_by: [{ pool: 'resource:browser', reason: 'RESOURCE_CLASS_LIMIT', limit: 4, active: 3 }],
          }),
        ],
        tenant_id: 'acme',
      }),
    )
    api.loadResourceClasses.mockResolvedValue(ok({ resource_classes: CLASSES }))

    render(<AgentsScreen onOpen={() => {}} />)

    const why = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.row .why')
      expect(el).not.toBeNull()
      return el!
    })
    expect(why.textContent ?? '').toMatch(/busy platform-wide.*\(3\/4\)/)
    expect(why.textContent ?? '').not.toMatch(NEVER)
  })
})

// ---------------------------------------------------------------------------
// The agent inspector (AgentDetail.tsx)
// ---------------------------------------------------------------------------

describe('the agent inspector, the same browser task', () => {
  function run(t: Task): AgentRun {
    return {
      task: t,
      events: [],
      eventsDetail: null,
      attempts: [attempt(1)],
      attemptsDetail: null,
      classes: CLASSES,
      classesDetail: null,
      classesRouteMissing: false,
    }
  }

  it('says the task can never be admitted and a person has to raise it, in --warn', async () => {
    api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })

    const { container } = render(<Run run={run(browserTask())} />)

    const why = await waitFor(() => {
      const p = container.querySelector<HTMLElement>('.why-full')
      expect(p, 'the inspector drew no why sentence').not.toBeNull()
      return p!
    })
    expect(why.textContent ?? '').toMatch(NEVER)
    expect(why.textContent ?? '').toMatch(RAISE)
    expect(why.textContent ?? '').not.toMatch(/busy platform-wide/i)
    expect(why.classList.contains('is-warn'), 'can-never-be-admitted needs a person').toBe(true)
  })
})
