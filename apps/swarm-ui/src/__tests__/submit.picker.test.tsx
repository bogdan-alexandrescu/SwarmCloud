// ONE RUNNER PICKER ON BOTH SUBMIT FORMS (#114), AND SEND PANELS THAT LIST
// WHAT THE CLICK COMMITS TO (#115).
//
// THE DEFECT, #114. The task form's radios printed class, units, backend and
// "key needed" for every runner and the room for the chosen one only; the
// workflow step picked its runner from a bare `<select>` that printed nothing
// at all. So the two facts that decide whether a submission moves -- is there
// room for it, and does this tenant hold the key it needs -- were on screen
// for one runner at a time, or for none. `RunnerPicker` is the one control
// both forms draw now: every row carries its room (`headroomFor(p).agents`)
// and whether the key is present or missing (`credential_registered` from
// `/v1/providers`), and a disabled runner stays in the list with its reason.
//
// THE DEFECT, #115. The task panel listed runner, input and result; the
// workflow panel steps, stages and result. What the click spends -- the units,
// the room, the repository a pushing strategy writes to -- was elsewhere or
// nowhere.
//
// The hand-built cases render `RunnerPicker` with arguments, as
// submit.room.test.tsx renders `ProfileFacts`; the form cases run against the
// development fixture (mock 15, generic 10, claude-code 0, browser 0; codex
// disabled; anthropic key registered, openai not).

import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import { HELP } from '../help'
import { RunnerPicker, type ProviderKeys } from '../RunnerPicker'
import { SubmitScreen, repositorySlug } from '../Submit'
import { SubmitWorkflowScreen } from '../SubmitWorkflow'
import type { ProfileAdmission, RunnerProfile } from '../types'

const WAIT = { timeout: 4000 }

const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

function admission(over: Partial<ProfileAdmission> = {}): ProfileAdmission {
  return {
    units: 1, headroom: 4, basis: 'measured', blockers: [], binding: [],
    counterfactual: [], complete: true, unread: [], uncapped: [], ...over,
  }
}

function profile(over: Partial<RunnerProfile> = {}): RunnerProfile {
  return {
    resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'anthropic', units: 1,
    pools: ['global', 'tenant:eng', 'resource:standard'], admission: admission(), ...over,
  }
}

const READ: ProviderKeys = { kind: 'read', registered: new Map([['anthropic', true], ['openai', false]]) }

function picker(profiles: Array<[string, RunnerProfile]>, keys: ProviderKeys = READ) {
  return render(
    <RunnerPicker group="t" label="runner" profiles={profiles} chosen="" keys={keys} onPick={() => {}} />,
  ).container as HTMLElement
}

/** One runner's row, found by its radio's value. */
function row(root: HTMLElement, name: string, group = 'runner-profile'): HTMLElement {
  const radio = root.querySelector<HTMLInputElement>(`input[type="radio"][name="${group}"][value="${name}"]`)
  expect(radio, `no radio for ${name} in ${group}`).not.toBeNull()
  return radio!.closest('label') as HTMLElement
}

// ---------------------------------------------------------------------------
// #114 -- the picker, by hand
// ---------------------------------------------------------------------------

describe('RunnerPicker: every row says its room and its key', () => {
  it('prints the measured room of every runner, not only the chosen one', () => {
    const el = picker([
      ['a', profile({ admission: admission({ headroom: 4 }) })],
      ['b', profile({ admission: admission({ headroom: 0 }) })],
    ])
    expect(visible(row(el, 'a', 't').querySelector('.sbf-runner-room'))).toBe('room for 4 more')
    // A measured zero is a digit: it is the answer, not an absence.
    expect(visible(row(el, 'b', 't').querySelector('.sbf-runner-room'))).toBe('room for 0 more')
  })

  it('draws an unread room as the unread mark and a pool count, with no room figure (TS-23)', () => {
    const el = picker([
      ['a', profile({ admission: admission({ headroom: null, basis: 'unknown', complete: false, unread: ['global'] }) })],
    ])
    const room = row(el, 'a', 't').querySelector('.sbf-runner-room')!
    const mark = room.querySelector('.ctl-mark.is-unread')
    expect(mark, 'an unread room is drawn without the unread mark').not.toBeNull()
    expect(mark!.getAttribute('aria-label')).toBe(HELP['room-unknown-not-zero'].short)
    expect(visible(room)).toBe('not read room unknown · 1 of 3 pools could not be read')
    expect(visible(room).replace('1 of 3 pools', ''), 'a figure in the slot of a room nobody measured').not.toMatch(/\d/)
  })

  it('says key present, key missing, or that no key is needed, from the providers read', () => {
    const el = picker([
      ['a', profile({ provider: 'anthropic' })],
      ['b', profile({ provider: 'openai' })],
      ['c', profile({ provider: null })],
    ])
    expect(visible(row(el, 'a', 't').querySelector('.sbf-runner-key'))).toBe('anthropic key present')
    expect(visible(row(el, 'b', 't').querySelector('.sbf-runner-key'))).toBe('openai key missing')
    expect(visible(row(el, 'c', 't').querySelector('.sbf-runner-key'))).toBe('no key needed')
  })

  it('never says present or missing when the providers read failed', () => {
    const el = picker([['a', profile({ provider: 'anthropic' })]], { kind: 'unread', detail: 'HTTP 503' })
    const key = row(el, 'a', 't').querySelector('.sbf-runner-key')!
    expect(key.querySelector('.ctl-mark.is-unread'), 'a failed read is drawn as a fact').not.toBeNull()
    expect(visible(key)).not.toMatch(/present|missing/)
  })

  it('keeps a disabled runner in the list, refused, with its reason', () => {
    const el = picker([['a', profile({ available: false, disabled_reason: 'switched off. Use b.' })]])
    const r = row(el, 'a', 't')
    expect(r.querySelector('input')!.disabled).toBe(true)
    expect(visible(r.querySelector('.sbf-runner-off'))).toBe('switched off. Use b.')
  })
})

// ---------------------------------------------------------------------------
// #114 -- both forms draw it
// ---------------------------------------------------------------------------

describe('both Submit forms pick a runner from the same picker', () => {
  it('the task form shows room and key on every row once the providers read lands', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Submit one task' }, WAIT)
    await waitFor(() => expect(visible(row(container, 'claude-code').querySelector('.sbf-runner-key'))).toBe('anthropic key present'), WAIT)
    expect(visible(row(container, 'claude-code').querySelector('.sbf-runner-room'))).toBe('room for 0 more')
    expect(visible(row(container, 'mock').querySelector('.sbf-runner-room'))).toBe('room for 15 more')
    expect(visible(row(container, 'mock').querySelector('.sbf-runner-key'))).toBe('no key needed')
    const codex = row(container, 'codex')
    expect(visible(codex.querySelector('.sbf-runner-key'))).toBe('openai key missing')
    expect(codex.querySelector('input')!.disabled).toBe(true)
    expect(visible(codex.querySelector('.sbf-runner-off'))).toMatch(/Use claude-code/)
  })

  it('a workflow step picks from the picker, not a bare select, and offers the disabled runner with its reason', async () => {
    const { container } = render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
    const step = container.querySelector<HTMLElement>('.wfb-step')!
    expect(step.querySelector('select'), 'the step still picks its runner from a bare <select>').toBeNull()
    const group = step.querySelector<HTMLInputElement>('input[type="radio"]')!.name
    await waitFor(() => expect(visible(row(step, 'claude-code', group).querySelector('.sbf-runner-key'))).toBe('anthropic key present'), WAIT)
    expect(visible(row(step, 'mock', group).querySelector('.sbf-runner-room'))).toBe('room for 15 more')
    const codex = row(step, 'codex', group)
    expect(codex.querySelector('input')!.disabled).toBe(true)
    expect(visible(codex.querySelector('.sbf-runner-off'))).toMatch(/Use claude-code/)
    // Picking changes the step's runner, as the select did.
    fireEvent.click(row(step, 'claude-code', group).querySelector('input')!)
    expect(visible(step.querySelector('.wfb-cost'))).toMatch(/^standard · 1 unit/)
    expect(within(container.querySelector<HTMLElement>('.sbf-send')!).getByRole('button', { name: 'claude-code-1' })).toBeTruthy()
  })

  it('two steps are two radio groups, so picking in one leaves the other alone', async () => {
    const { container } = render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
    fireEvent.click(container.querySelector<HTMLButtonElement>('button.wfb-add.is-stage')!)
    const [a, b] = [...container.querySelectorAll<HTMLElement>('.wfb-step')]
    const ga = a!.querySelector<HTMLInputElement>('input[type="radio"]')!.name
    const gb = b!.querySelector<HTMLInputElement>('input[type="radio"]')!.name
    expect(ga).not.toBe(gb)
    // #118: neither step starts with a runner.
    fireEvent.click(row(a!, 'browser', ga).querySelector('input')!)
    fireEvent.click(row(b!, 'mock', gb).querySelector('input')!)
    expect(row(a!, 'mock', ga).querySelector('input')!.checked).toBe(false)
    expect(row(a!, 'browser', ga).querySelector('input')!.checked).toBe(true)
    expect(row(b!, 'mock', gb).querySelector('input')!.checked).toBe(true)
    expect(row(b!, 'browser', gb).querySelector('input')!.checked).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// #115 -- the send panels
// ---------------------------------------------------------------------------

/** The value of one `.ctl-fact` in the send panel, by its key. */
function fact(root: HTMLElement, key: string): HTMLElement | null {
  const facts = [...root.querySelectorAll<HTMLElement>('.sbf-send .ctl-fact')]
  return facts.find((f) => f.querySelector('b')?.textContent === key) ?? null
}
/** A fact's value: its text after its `<b>` key. */
const valueOf = (f: HTMLElement | null) =>
  visible(f).slice((f?.querySelector('b')?.textContent ?? '').length).trim()

describe('the task send panel lists what the click commits to', () => {
  it('names the cost and the room of the chosen runner, in the decided phrase', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Submit one task' }, WAIT)
    fireEvent.click(container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="browser"]')!)
    expect(valueOf(fact(container, 'cost'))).toBe('2 units in each of 7 pools')
    expect(valueOf(fact(container, 'cost')), '"x" reads as a total').not.toMatch(/×|\bx\b/)
    expect(valueOf(fact(container, 'room'))).toBe('room for 0 more')
    fireEvent.click(container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="mock"]')!)
    expect(valueOf(fact(container, 'cost'))).toBe('1 unit in each of 5 pools')
    expect(valueOf(fact(container, 'room'))).toBe('room for 15 more')
  })

  it('draws the cost and room as undecided before a runner is chosen', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Submit one task' }, WAIT)
    for (const k of ['cost', 'room']) {
      const f = fact(container, k)
      expect(f, `no ${k} fact in the panel`).not.toBeNull()
      expect(f!.classList.contains('is-absent')).toBe(true)
      expect(valueOf(f)).not.toMatch(/\d/)
    }
  })

  it('names the repository as owner/repo only when the strategy pushes', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Submit one task' }, WAIT)
    // The default strategy publishes nothing, so there is no repository to name.
    expect(fact(container, 'repository')).toBeNull()
    const pr = container.querySelector<HTMLInputElement>('input[type="radio"][value="direct-pr"]')
    expect(pr, 'the dispatch control offers no direct-pr on the task form').not.toBeNull()
    fireEvent.click(pr!)
    expect(fact(container, 'repository'), 'a pushing strategy and no repository in the panel').not.toBeNull()
    expect(valueOf(fact(container, 'repository'))).toMatch(/none/)
    const url = container.querySelector<HTMLInputElement>('input[placeholder="https://github.com/owner/repo.git"]')!
    fireEvent.change(url, { target: { value: 'https://github.com/acme/widgets.git' } })
    expect(valueOf(fact(container, 'repository'))).toBe('acme/widgets')
  })

  it('reads owner/repo off every scheme the API accepts', () => {
    expect(repositorySlug('https://github.com/acme/widgets.git')).toBe('acme/widgets')
    expect(repositorySlug('https://github.com/acme/widgets')).toBe('acme/widgets')
    expect(repositorySlug('git@github.com:acme/widgets.git')).toBe('acme/widgets')
    expect(repositorySlug('ssh://git@github.com/acme/widgets.git')).toBe('acme/widgets')
    // Nothing recognisable: the URL as sent, never a guess.
    expect(repositorySlug('https://example.com')).toBe('https://example.com')
  })
})

describe('the workflow send panel lists what the click commits to', () => {
  it('sums the units over every step and counts the steps in each stage', async () => {
    const { container } = render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
    // #118: no runner yet, so no weight to add -- unknown, never zero.
    expect(fact(container, 'units')!.classList.contains('is-absent')).toBe(true)
    expect(valueOf(fact(container, 'units'))).not.toMatch(/\d/)
    expect(valueOf(fact(container, 'stages'))).toBe('1 · 1 step')
    const pickIn = (step: HTMLElement, name: string) => {
      const group = step.querySelector<HTMLInputElement>('input[type="radio"]')!.name
      fireEvent.click(row(step, name, group).querySelector('input')!)
    }
    // One browser step: two units.
    pickIn(container.querySelector<HTMLElement>('.wfb-step')!, 'browser')
    expect(valueOf(fact(container, 'units'))).toBe('2 units in total')
    fireEvent.click(container.querySelector<HTMLButtonElement>('button.wfb-add.is-stage')!)
    const second = container.querySelectorAll<HTMLElement>('.wfb-stage')[1]!
    fireEvent.click(second.querySelector<HTMLButtonElement>('.wfb-steps > button.sbf-addstep')!)
    for (const s of [...second.querySelectorAll<HTMLElement>('.wfb-step')]) pickIn(s, 'browser')
    expect(valueOf(fact(container, 'units'))).toBe('6 units in total')
    // A mock step weighs one: the sum follows the runner, not the step count.
    const last = [...container.querySelectorAll<HTMLElement>('.wfb-step')].at(-1)!
    const group = last.querySelector<HTMLInputElement>('input[type="radio"]')!.name
    fireEvent.click(row(last, 'mock', group).querySelector('input')!)
    expect(valueOf(fact(container, 'units'))).toBe('5 units in total')
    expect(valueOf(fact(container, 'stages'))).toBe('2 · 1 step, then 2 steps')
  })
})
