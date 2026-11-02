// QA1B-11 (part of #2596): QA1-FIX-22's Runs.tsx half (runs-submit-27),
// applied by pass 1b.
//
//   * The Linked card's Planner fact was the raw id `task_planner1` as its
//     link. The link says `planner agent`; the id follows it as a note.
//   * `mode not stated` was a mono chip, drawn like the planner's real call.
//     It is a note now; `single agent` and `workflow` stay chips.
//
// BREAK IT: draw `{run.planner_task_id}` as the link's text again, or put
// `mode not stated` back in the `<Chip>` -- a case below turns red.

import { render, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { FULL_PLAN, issueRun } from './runfixture'

const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const JSON_HEADERS = { 'content-type': 'application/json' }
const WAIT = { timeout: 4000 }

afterEach(() => {
  vi.unstubAllEnvs()
})

async function mount(over: Record<string, unknown> = {}) {
  const run = issueRun(over)
  const routes: Record<string, unknown> = { [`/v1/runs/${run.id}`]: { run } }
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url in routes) return new Response(JSON.stringify(routes[url]), { status: 200, headers: JSON_HEADERS })
    return new Response(JSON.stringify({ code: 'not_found', message: url }), { status: 404, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const { container } = render(<RunsScreen view={`run=${run.id}`} go={vi.fn()} />)
  await waitFor(() => expect(container.querySelector('section[aria-label="Linked"]')).not.toBeNull(), WAIT)
  return container
}

function planner(container: HTMLElement): HTMLElement {
  const linked = within(container).getByRole('region', { name: 'Linked' })
  const fact = [...linked.querySelectorAll<HTMLElement>('.ctl-fact')].find((f) => f.querySelector('b')?.textContent === 'Planner')
  expect(fact).toBeTruthy()
  return fact!
}

describe('runs-submit-27: the planner is named, not shown as its raw id', () => {
  it('links `planner agent` to the planner task and gives the id after it', async () => {
    const container = await mount()
    const fact = planner(container)
    const link = fact.querySelector<HTMLAnchorElement>('a')!
    // MUTATION: restore `{run.planner_task_id}` as the link's text and this reads the id.
    expect(visible(link)).toBe('planner agent')
    expect(link.getAttribute('href')).toBe('/agents/recent/task_planner1')
    const id = fact.querySelector<HTMLElement>('.sb-note')!
    expect(visible(id)).toBe('task_planner1')
    // The id is cut to the card like the link it was (D3), whole in its title.
    expect(id.classList.contains('rn-id')).toBe(true)
    expect(id.getAttribute('title')).toBe('task_planner1')
    expect(visible(fact)).toBe('Plannerplanner agenttask_planner1')
    // The link is words, so it is neither cut nor set in mono.
    expect(link.classList.contains('rn-id')).toBe(false)
    expect(link.classList.contains('mono')).toBe(false)
  })

  it('still says `not recorded`, with no id note, when the run records no planner', async () => {
    const container = await mount({ planner_task_id: '' })
    const fact = planner(container)
    expect(fact.querySelector('a')).toBeNull()
    expect(fact.querySelector('.sb-note')).toBeNull()
    expect(visible(fact)).toContain('not recorded')
  })
})

describe('runs-submit-27: `mode not stated` is not a chip', () => {
  it('draws no chip for an unstated mode', async () => {
    const container = await mount({ plan: { ...FULL_PLAN, mode: undefined } })
    const facts = await waitFor(() => {
      const f = container.querySelector<HTMLElement>('.rn-plan-facts')
      expect(f).not.toBeNull()
      return f!
    }, WAIT)
    const chips = [...facts.querySelectorAll('.c-chip')].map((c) => visible(c))
    // MUTATION: put `mode not stated` back in the `<Chip>` and it is among the chips.
    expect(chips).not.toContain('mode not stated')
    // QA2-FIX-20 (runs2-11): nor is it a note any more; an unstated mode says nothing.
    expect(visible(facts)).not.toContain('mode not stated')
  })

  it('keeps a stated mode as a chip', async () => {
    const container = await mount({ plan: FULL_PLAN })
    const facts = await waitFor(() => {
      const f = container.querySelector<HTMLElement>('.rn-plan-facts')
      expect(f).not.toBeNull()
      return f!
    }, WAIT)
    expect([...facts.querySelectorAll('.c-chip')].map((c) => visible(c))).toContain('workflow')
  })
})
