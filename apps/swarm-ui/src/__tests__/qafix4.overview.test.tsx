/**
 * VISUAL QA RUN 4, LANE QAFIX4-OVERVIEW (part of #1680; run
 * wf_ecd935541ced4a539540, the fixture build of main at fabe4d6c, 1440 and
 * 390 px, light and dark).
 *
 *   N10 (overview-automate-006, the mock part) The mock profile's Headroom
 *       tile read "binds global + standard + C…": the CLOUD_RUN_JOB backend
 *       pool, binding as much as the other two, was cut off the tile and
 *       lived only in its hover title. The binding line now wraps between
 *       pool names, never inside one.
 *   N11 (flow-workflow-graph-light-04, dark-04) The Needs a look card named
 *       wf_5e5ad3b6f7da4299a839, but the whole card was one link to the
 *       Agents list, so the run reached the workflow through Work ›
 *       Workflows. Each workflow the card names is now a link of its own,
 *       beside -- never inside -- the card's 'Agents · FAILED' link.
 *
 * Kept as built (owner decisions 2026-10-21): the disabled tile's two-line
 * clamp on a desktop, whole on a phone.
 *
 * Each assertion names the mutation that turns it red.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import { CheckCard, ProfileTile, toPath } from '../Overview'
import { deriveChecks, type CheckInputs, type Problem } from '../checks'
import type { Result } from '../fetch'
import type { Capacity, Task, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task } from './runfixture'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const MIN = 60_000

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
})

const text = (el: Element | null | undefined): string => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

type Profile = Capacity['runner_profiles'][string]

/** The fixture's mock profile (api.ts FIXTURE_ADMISSION.mock): three pools bind it. */
const MOCK = {
  resource_class: 'standard',
  backend: 'CLOUD_RUN_JOB',
  provider: null,
  units: 1,
  pools: ['global', 'tenant:u-bogdan', 'resource:standard', 'runner:mock', 'backend:CLOUD_RUN_JOB'],
  admission: {
    units: 1,
    headroom: 15,
    basis: 'measured',
    blockers: [],
    binding: ['global', 'resource:standard', 'backend:CLOUD_RUN_JOB'],
    counterfactual: [],
    complete: true,
    unread: [],
    uncapped: [],
  },
} as unknown as Profile

describe('N10: the mock Headroom tile shows every pool that binds it', () => {
  it('holds each binding pool’s label in the line, each one unbreakable', () => {
    const { container } = render(<ProfileTile name="mock" profile={MOCK} byName={new Map()} tenant="u-bogdan" />)
    const tile = container.querySelector('.ov-hp')!
    const line = tile.querySelector('small')!
    // MUTATION: drop a pool from the binding list the tile renders.
    for (const label of ['global', 'standard', 'CLOUD_RUN_JOB']) expect(text(line)).toContain(label)
    expect(text(line)).toBe('binds global + standard + CLOUD_RUN_JOB')
    // Run 4 saw "binds global + standard + C…" at 1440.
    // MUTATION: put `white-space: nowrap` back on the binding line (`.ov-hp small.ov-hp-binds`).
    expect(painted(line, 'white-space', WIDE)).not.toBe('nowrap')
    expect(painted(line, 'text-overflow', WIDE)).not.toBe('ellipsis')
    // A line break falls at ' + ', never inside a pool's name.
    // MUTATION: render the binding words as one string rather than one element per pool.
    const pools = [...line.querySelectorAll('.ov-hp-pool')]
    expect(pools.map(text)).toEqual(['global', 'standard', 'CLOUD_RUN_JOB'])
    // MUTATION: drop `white-space: nowrap` from `.ov-hp-pool`.
    for (const p of pools) expect(painted(p, 'white-space', WIDE), text(p)).toBe('nowrap')
    // The tile's title still holds the whole list, by full pool name.
    // MUTATION: drop the "Bound by" clause from the tile's long form.
    expect(tile.getAttribute('title')).toContain('Bound by global and resource:standard and backend:CLOUD_RUN_JOB.')
  })

  it('leaves the other tiles’ one-line words as they were', () => {
    const uncapped = {
      ...MOCK,
      admission: { ...(MOCK.admission as object), headroom: null, basis: 'uncapped', binding: [] },
    } as unknown as Profile
    const { container } = render(<ProfileTile name="open" profile={uncapped} byName={new Map()} tenant="u-bogdan" />)
    const line = container.querySelector('.ov-hp small')!
    expect(text(line)).toBe('no pool caps it')
    // MUTATION: let every tile's line wrap (`white-space: normal` on `.ov-hp small`).
    expect(painted(line, 'white-space', WIDE)).toBe('nowrap')
    expect(painted(line, 'text-overflow', WIDE)).toBe('ellipsis')
    expect(line.classList.contains('ov-hp-binds')).toBe(false)
  })

  it('keeps the disabled tile’s reason clamped to two lines on a desktop, whole on a phone', () => {
    const off = { ...MOCK, available: false, disabled_reason: 'the merge chain (#295) is disabled until its review step stops looping' } as unknown as Profile
    const { container } = render(<ProfileTile name="merge-chain" profile={off} byName={new Map()} tenant="u-bogdan" />)
    const small = container.querySelector('.ov-hp.is-off small')!
    // MUTATION: let the binding rule's selector reach the disabled tile's reason.
    expect(painted(small, '-webkit-line-clamp', WIDE)).toBe('2')
    expect(painted(small, 'display', WIDE)).toBe('-webkit-box')
    expect(painted(small, 'overflow', WIDE)).toBe('hidden')
    // MUTATION: drop the phone block's unclamp in overview.css.
    expect(painted(small, 'display', PHONE)).toBe('block')
  })
})

const WF_A = 'wf_5e5ad3b6f7da4299a839'
const WF_B = 'wf_0b1c2d3e4f5a6b7c8d9e'
const FAILED_LIST = toPath('#work/running/recent/failed')

describe('N11: the failed-agents card links each workflow it names', () => {
  function failuresProblem(tasks: Task[]): Problem {
    const now = Date.now()
    const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: now })
    const empty = { status: 'empty', fetchedAt: now } as const
    const c = {
      capacity: empty,
      tasks: ok({ tasks, next_page_token: null, tenant_id: 'eng' } as TaskPage),
      leases: empty,
      providers: empty,
      accounts: empty,
      stats: empty,
      workflows: empty,
    } as unknown as CheckInputs
    const check = deriveChecks(c, now).find((x) => x.label === 'Failures')!
    expect(check.status).toBe('found')
    return check.status === 'found' ? check.problems[0]! : (undefined as never)
  }
  const ago = (m: number): string => new Date(Date.now() - m * MIN).toISOString()
  const FAILED = [
    task({ id: 'tsk_f1', state: 'FAILED', workflow_id: WF_A, completed_at: ago(2), last_error: 'exit status 1' }),
    task({ id: 'tsk_f2', state: 'FAILED', workflow_id: WF_A, completed_at: ago(4) }),
    task({ id: 'tsk_f3', state: 'FAILED', workflow_id: WF_B, completed_at: ago(6) }),
  ]

  it('carries the workflows the headline names, each with its address', () => {
    const p = failuresProblem(FAILED)
    expect(p.headline).toContain(`2 in workflow ${WF_A}, 1 in ${WF_B}`)
    // MUTATION: leave `workflows` off the problem `failedProblem` builds.
    expect(p.workflows?.map((w) => w.id)).toEqual([WF_A, WF_B])
    // MUTATION: give a workflow the list's address rather than its own.
    expect(p.workflows?.map((w) => toPath(w.href))).toEqual([`/workflows/${WF_A}`, `/workflows/${WF_B}`])
    // The card's own link is unchanged: the page, then the state (QA-109).
    expect(p.linkLabel).toBe('Agents · FAILED')
    expect(p.href).toBe('#work/running/recent/failed')
  })

  it('draws one link per named workflow, none inside another, beside the card’s own link', () => {
    const p = failuresProblem(FAILED)
    const { container } = render(<CheckCard problem={p} />)
    for (const id of [WF_A, WF_B]) {
      // The id in the ids line, and (5b:wg-dark-D1, qafix5.overview-timeline)
      // a `Workflow <id> →` link beside the card's own.
      const links = [...container.querySelectorAll(`.ov-att-ids a[href="/workflows/${id}"]`)]
      // MUTATION: draw the ids line as plain text again.
      expect(links, id).toHaveLength(1)
      expect(text(links[0])).toBe(id)
    }
    // A link inside a link is invalid HTML, and a browser splits it.
    // MUTATION: keep the card's root `<a>` and put the workflow links inside it.
    expect(container.querySelectorAll('a a')).toHaveLength(0)
    // MUTATION: drop the card's main link, or point it anywhere but the failed list.
    const main = [...container.querySelectorAll(`a[href="${FAILED_LIST}"]`)]
    expect(main).toHaveLength(1)
    // MUTATION: change the main link's words.
    expect(text(main[0])).toContain('Agents · FAILED')
    // MUTATION: move the card's accessible name off its main link.
    // Its destination first (5b:wg-dark-D1), then the card's words.
    expect(main[0]!.getAttribute('aria-label')).toBe(`Agents · FAILED: ${p.headline}. ${p.detail}`)
    // The rest of the card opens the FIRST WORKFLOW, not the list (BGD's
    // decision of 2026-10-23, #1680 box 6343766999): the first named
    // workflow's link carries the stretched cover, and the list's link is a
    // plain link drawn above it.
    const [cover, second] = [...container.querySelectorAll('.ov-att-acts a.ov-att-wf')]
    expect(cover!.getAttribute('href')).toBe(`/workflows/${WF_A}`)
    // MUTATION: leave the stretched cover on the list's link.
    expect(painted(main[0]!, 'content', WIDE, 'after') ?? 'none').toBe('none')
    // MUTATION: drop `position: relative; z-index: 1` from the list link, so the cover swallows its clicks.
    expect(painted(main[0]!, 'position', WIDE)).toBe('relative')
    expect(painted(main[0]!, 'z-index', WIDE)).toBe('1')
    // MUTATION: drop the stretched cover from the first workflow's link, or keep it `position: relative`.
    expect(painted(cover!, 'position', WIDE)).not.toBe('relative')
    expect(painted(cover!, 'content', WIDE, 'after')).not.toBeNull()
    expect(painted(cover!, 'position', WIDE, 'after')).toBe('absolute')
    // MUTATION: give the second workflow the cover too, or drop its `position: relative`.
    expect(painted(second!, 'content', WIDE, 'after') ?? 'none').toBe('none')
    expect(painted(second!, 'position', WIDE)).toBe('relative')
    expect(painted(second!, 'z-index', WIDE)).toBe('1')
    // The ids line's links stay above the cover.
    // MUTATION: drop `position: relative` from the ids line's links.
    const idLink = container.querySelector(`.ov-att-ids a[href="/workflows/${WF_B}"]`)!
    expect(painted(idLink, 'position', WIDE)).toBe('relative')
    expect(painted(idLink, 'z-index', WIDE)).toBe('1')
  })

  it('keeps a card that names no workflow as one link', () => {
    const p: Problem = {
      severity: 'bad',
      n: 2,
      headline: '2 failed agents among the 26 most recent · newest 4m ago',
      detail: 'Terminal: nothing will retry them. 2 used every attempt. Newest: exit status 1',
      href: '#work/running/recent/failed',
      linkLabel: 'Agents · FAILED',
    }
    const { container } = render(<CheckCard problem={p} />)
    // MUTATION: draw every card as the split shape, root `<div>` and a link inside.
    const root = container.firstElementChild!
    expect(root.tagName).toBe('A')
    expect(root.className).toBe('ov-att is-bad')
    expect(root.getAttribute('href')).toBe(FAILED_LIST)
    expect(root.getAttribute('aria-label')).toBe(`${p.headline}. ${p.detail}`)
    expect(container.querySelectorAll('a')).toHaveLength(1)
    // The same with an empty list: nothing to link, nothing changes.
    // MUTATION: split the card whenever `workflows` is present, empty or not.
    cleanup()
    const again = render(<CheckCard problem={{ ...p, workflows: [] }} />).container
    expect(again.querySelectorAll('a')).toHaveLength(1)
    expect(again.firstElementChild!.tagName).toBe('A')
  })
})
