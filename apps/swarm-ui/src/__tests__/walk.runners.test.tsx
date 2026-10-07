/**
 * WALKTHROUGH D (owner, 2026-10-03, live console at 1440x900): only usable
 * options.
 *
 * Measured: /submit/issue step 2 showed seven disabled runner cards with long
 * orange explanations while only claude-code is allowed.
 *
 * A runner list whose entries are mostly disabled shows the usable choice(s)
 * only, and "Other runners (N unavailable)" behind a disclosure that lists
 * each with a one-line reason; the long reason is that line's tooltip and the
 * why is behind the list's `?`. The issue form, the task form (`RunnerPicker`)
 * and the workflow form's per-step select (`RunnerSelect`) follow the one rule
 * (`runnerSplit`). A list with most entries usable keeps every card in place.
 *
 * MUTATIONS: draw the disabled cards in the radio group again, or drop the
 * optgroup from the select -- each turns a case red.
 */
import { render, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { App } from '../App'
import { loadCapacity } from '../api'
import { RunnerPicker, RunnerSelect, runnerSplit } from '../RunnerPicker'
import type { RunnerProfile } from '../types'


afterEach(() => {
  window.history.replaceState(null, '', '/')
})

async function base(): Promise<RunnerProfile> {
  const r = await loadCapacity()
  if (r.status !== 'ok' && r.status !== 'stale') throw new Error('the capacity fixture did not load')
  return Object.values(r.data.runner_profiles)[0]!
}

function catalogue(p: RunnerProfile, offNames: readonly string[], all: readonly string[]): [string, RunnerProfile][] {
  const reason = ['the merge chain (#', '295) is disabled for every tenant until signed step specs (#', '342) land'].join('')
  return all.map((n) => [n, offNames.includes(n) ? { ...p, available: false, disabled_reason: reason } : { ...p, available: true, disabled_reason: '' }])
}

const KEYS = { kind: 'read', registered: new Map<string, boolean>() } as const

describe('D: the issue form shows the one runner it can use', () => {
  it('draws claude-code alone, and the others behind "Other runners (N, not used for issue runs)" with a one-line reason each', async () => {
    window.history.replaceState(null, '', '/submit/issue')
    render(<App />)
    const group = await waitFor(() => {
      const g = document.querySelector<HTMLElement>('[role="radiogroup"][aria-label="runner"]')
      expect(g).not.toBeNull()
      return g!
    })
    const radios = within(group).getAllByRole('radio')
    expect(radios.map((r) => (r as HTMLInputElement).value)).toEqual(['claude-code'])
    expect(group.textContent, 'a disabled explanation in the usable list').not.toMatch(/disabled:/)
    const more = document.querySelector<HTMLDetailsElement>('details.sbf-runners-more')
    expect(more, 'no disclosure for the unavailable runners').not.toBeNull()
    expect(more!.open, 'the unavailable runners open by default').toBe(false)
    const items = [...more!.querySelectorAll('li')]
    // Not "unavailable" (QA G4-30): browser or mock can start now; issue runs do not use them.
    expect(more!.querySelector('summary')!.textContent).toBe(`Other runners (${items.length}, not used for issue runs)`)
    expect(items.length).toBeGreaterThan(0)
    // One short line each: the issue form's own reason, or a platform reason
    // shortened (a runner the platform disables keeps its reason, as a tooltip).
    const reasons = items.map((li) => li.querySelector('.sbf-runner-why')?.textContent ?? '')
    expect(reasons).toContain('Issue runs always use claude-code')
    for (const [i, li] of items.entries()) {
      expect(reasons[i]!.length, reasons[i]).toBeLessThanOrEqual(48)
      expect(reasons[i], 'an implementation detail in a reason').not.toMatch(/#\d|\/v1\/|disabled:/)
      expect(li.querySelector('input'), 'an unavailable runner is still a choice').toBeNull()
    }
  })
})

describe('D: one rule for every runner list', () => {
  it('splits only when most entries are disabled, and keeps the chosen one in view', async () => {
    const p = await base()
    const most = runnerSplit(catalogue(p, ['b', 'c', 'd'], ['a', 'b', 'c', 'd']), 'a')
    expect(most.shown.map(([n]) => n)).toEqual(['a'])
    expect(most.held.map(([n]) => n)).toEqual(['b', 'c', 'd'])
    const few = runnerSplit(catalogue(p, ['d'], ['a', 'b', 'c', 'd']), 'a')
    expect(few.shown.map(([n]) => n)).toEqual(['a', 'b', 'c', 'd'])
    expect(few.held).toEqual([])
  })

  it('draws the task form’s held runners behind the disclosure, with a short reason and the long one in the tooltip', async () => {
    const p = await base()
    render(<RunnerPicker group="g" label="runner" profiles={catalogue(p, ['b', 'c', 'd'], ['a', 'b', 'c', 'd'])} chosen="a" keys={KEYS} onPick={() => {}} />)
    expect(document.querySelectorAll('[role="radiogroup"] input[type="radio"]')).toHaveLength(1)
    const li = [...document.querySelectorAll('details.sbf-runners-more li')]
    expect(li).toHaveLength(3)
    const why = li[0]!.querySelector('.sbf-runner-why')!
    expect(why.textContent).toBe('Not enabled yet')
    expect(li[0]!.getAttribute('title')).toMatch(/merge chain/)
    // And where most are usable, the disabled one stays a card, with the short reason.
    document.body.innerHTML = ''
    render(<RunnerPicker group="g" label="runner" profiles={catalogue(p, ['d'], ['a', 'b', 'c', 'd'])} chosen="a" keys={KEYS} onPick={() => {}} />)
    expect(document.querySelector('details.sbf-runners-more')).toBeNull()
    expect(document.querySelectorAll('[role="radiogroup"] input[type="radio"]')).toHaveLength(4)
    expect(document.querySelector('.sbf-runner-off')?.textContent).toBe('Not enabled yet')
  })

  it('puts the workflow step select’s held runners in their own group', async () => {
    const p = await base()
    render(<RunnerSelect id="s" label="step runner" profiles={catalogue(p, ['b', 'c', 'd'], ['a', 'b', 'c', 'd'])} chosen="a" onPick={() => {}} />)
    const select = document.querySelector('select')!
    const top = [...select.children].filter((c) => c.tagName === 'OPTION').map((o) => (o as HTMLOptionElement).value)
    expect(top).toEqual(['', 'a'])
    const group = select.querySelector('optgroup')!
    expect(group.getAttribute('label')).toBe('Other runners (3 unavailable)')
    expect([...group.querySelectorAll('option')].every((o) => o.disabled)).toBe(true)
  })
})
