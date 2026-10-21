/**
 * LANE QAFIX-SUBMIT-OTHER (part of #1680): the first console visual-QA run
 * (wf_9e9bf74834634db7bcc3, 2026-10-20, offline preview at 1440 and 390 px)
 * on Submit from an issue, Submit a workflow and the runner picker.
 *
 *   QA-072  the issue form's "Fix rounds" field showed its default 3 at the
 *           not-reached step's .55 opacity inside a `--line` hairline, so an
 *           entered value read as a placeholder.
 *   QA-073  the issue form's send summary set values at 14 px, identifiers at
 *           13 px mono and the absent value in italic, and "lands as" wrapped
 *           its long value right-aligned beside the key.
 *   QA-074  the workflow form's fixed carrier was a disabled select still
 *           drawn as one -- filled, edged and chevroned -- so it looked
 *           choosable. The owner chose (2026-10-20, lane QAFIX-OWNER-PICKS)
 *           plain text over the restyled select: no control at all.
 *   QA-068  the task form's runner cards did not fill their grid row, so the
 *           bottoms of two cards side by side stepped.
 *   QA-069  "room unknown · 7 of 7 pools could not be read" broke so that
 *           "be read" sat alone on the last line.
 *   QA-105  a refused runner's card said "openai key missing" in sight and
 *           the platform's refusal only on hover: the key is moot for a
 *           runner the platform will not start.
 *
 * Layout and colour are read off the shipped sheets through `painted()`, as
 * access.page.test.tsx does; copy and counts off the rendered text.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { RunnerPicker, type ProviderKeys } from '../RunnerPicker'
import type { ProfileAdmission, RunnerProfile } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const ENVS: ReadonlyArray<readonly [string, CascadeEnv]> = [['1440', WIDE], ['390', PHONE]]

const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

afterEach(() => {
  document.body.innerHTML = ''
})

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

function picker(profiles: Array<[string, RunnerProfile]>) {
  return render(
    <RunnerPicker group="t" label="runner" profiles={profiles} chosen="" keys={READ} onPick={() => {}} />,
  ).container as HTMLElement
}

const card = (root: HTMLElement, name: string) =>
  root.querySelector<HTMLInputElement>(`input[type="radio"][value="${name}"]`)!.closest('label') as HTMLElement

async function issueForm() {
  const { IssueSubmitScreen } = await import('../IssueSubmit')
  const utils = render(<IssueSubmitScreen go={() => {}} />)
  await screen.findByRole('heading', { name: 'Submit from a GitHub issue', level: 1 }, WAIT)
  await screen.findByRole('radiogroup', { name: 'runner' }, WAIT)
  return utils.container as HTMLElement
}

const fact = (send: Element, key: string) =>
  [...send.querySelectorAll('.ctl-fact')].find((li) => visible(li.querySelector(':scope > b')) === key)!

describe('QA-072: the fix-rounds default reads as an entered value', () => {
  it('keeps full strength in a step not reached yet, inside the control edge', async () => {
    await issueForm()
    const field = screen.getByLabelText('Fix rounds when checks go red') as HTMLInputElement
    expect(field.value).toBe('3')
    // Precondition: the step is the dimmed, not-reached one the operator saw.
    expect(field.closest('.sb-step.is-dim'), 'the step is no longer dimmed before a read').not.toBeNull()
    for (const [w, env] of ENVS) {
      // MUTATION: drop `.sb-step.is-dim .in-rounds { opacity: 1 }` -> the .55 of the step's controls wins.
      expect(painted(field, 'opacity', env), `${w}: the value is faded like a placeholder`).toBe('1')
      // MUTATION: put `border: 1px solid var(--line)` back on `.in-rounds` -> the 1.3:1 hairline.
      expect(painted(field, ['border-color', 'border'], env), `${w}: the field edge is not the control edge`).toContain('var(--ctl-bd)')
      // MUTATION: set `.in-rounds { color: var(--text-dim) }` -> grey ink.
      expect(painted(field, 'color', env), `${w}: the value is not in the text ink`).toBe('var(--text)')
    }
  })
})

describe('QA-073: the issue form\'s summary is one type treatment', () => {
  it('sets every value at the fact\'s own size and the absent value upright', async () => {
    const container = await issueForm()
    const send = container.querySelector('.sbf-send.in-send')!
    expect(visible(send.querySelector('h2'))).toBe('Not ready to send')
    const absent = fact(send, 'issue').querySelector('.ctl-em')!
    expect(visible(absent)).toBe('— read one first')
    const runner = fact(send, 'runner').querySelector('code')!
    const plan = fact(send, 'plan')
    for (const [w, env] of ENVS) {
      const size = painted(plan, 'font-size', env)
      // MUTATION: drop `.sbf-send.in-send .ctl-fact { font-size: var(--t-meta) }` -> 14 px text beside 13 px code.
      expect(size, `${w}: the plain values' size`).toBe('var(--t-meta)')
      // MUTATION: give the code its own size again -> two sizes in one column.
      expect(painted(runner, 'font-size', env) ?? 'inherit', `${w}: the runner's size`).toMatch(/^(inherit|1em|var\(--t-meta\))$/)
      // MUTATION: drop `font-style: normal` from the absent value -> the UA's italic.
      expect(painted(absent, 'font-style', env), `${w}: the absent value is italic`).toBe('normal')
    }
  })

  it('stacks "lands as" under its key, left-aligned, rather than wrapping it beside the key', async () => {
    const container = await issueForm()
    const send = container.querySelector('.sbf-send.in-send')!
    const lands = fact(send, 'lands as')
    expect(visible(lands)).toMatch(/^lands as\s*PLANNING, then PLANNED/)
    for (const [w, env] of ENVS) {
      // MUTATION: drop the `in-fact-long` class (or its rule) -> the value wraps right-aligned beside the key.
      expect(painted(lands, 'flex-direction', env), `${w}: "lands as" is beside its key`).toBe('column')
      expect(painted(lands, 'text-align', env), `${w}: "lands as" is right-aligned`).toBe('left')
      expect(painted(lands, 'align-items', env), `${w}: "lands as" is not drawn from the left edge`).toBe('flex-start')
    }
  })
})

describe('QA-074: the fixed carrier is a value, not a control (owner 2026-10-20)', () => {
  it('draws the label and `checkpoints (fixed)` as plain text, with its note, and no select', async () => {
    const { SubmitWorkflowScreen } = await import('../SubmitWorkflow')
    const { container } = render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
    // MUTATION: put the disabled `<select id="dsp-carrier">` back -- a screen
    // reader announces a disabled combobox again.
    expect(screen.queryByRole('combobox', { name: 'what carries work between steps' })).toBeNull()
    expect(container.querySelector('#dsp-carrier select, select#dsp-carrier')).toBeNull()
    const value = container.querySelector<HTMLElement>('#dsp-carrier')!
    // MUTATION: drop the plain-text value -- the form no longer says what it sends.
    expect(visible(value)).toBe('checkpoints (fixed)')
    // MUTATION: keep the label as a `<label for>` with no control to name.
    expect(container.querySelector('label[for="dsp-carrier"]')).toBeNull()
    const label = value.previousElementSibling!
    expect(visible(label)).toBe('what carries work between steps')
    // The note still follows the value, unchanged.
    expect(visible(value.nextElementSibling)).toMatch(/^not acted on yet/)
    for (const [w, env] of ENVS) {
      // MUTATION: drop `.dsp-fixed { margin: 0 }` -- the UA paragraph margin
      // pushes the value off its label.
      expect(painted(value, ['margin', 'margin-top'], env), `${w}: the value floats off its label`).toBe('0')
    }
  })
})

describe('QA-068: runner cards fill their grid row', () => {
  it('stretches each card to the height of its row', () => {
    const el = picker([
      ['a', profile({ admission: admission({ headroom: null, basis: 'unknown', complete: false, unread: ['global', 'tenant:eng', 'resource:standard'] }) })],
      ['b', profile()],
    ])
    const list = el.querySelector('.sbf-runners.is-cards')!
    const items = [...list.querySelectorAll(':scope > li')]
    expect(items).toHaveLength(2)
    for (const [w, env] of ENVS) {
      // A grid item stretches to its row unless told otherwise; nothing may tell it.
      expect(painted(list, 'align-items', env) ?? 'normal', `${w}: the grid stops its items stretching`).toMatch(/^(normal|stretch)$/)
      for (const li of items) {
        // MUTATION: drop `.sbf-runners.is-cards > li { display: flex }` -> the label is a block of its own height.
        expect(painted(li, 'display', env), `${w}: the row's cell does not pass its height on`).toBe('flex')
        // MUTATION: drop `flex: 1 1 auto` from the card -> it keeps its content's height inside the stretched cell.
        expect(painted(card(el, li.querySelector('input')!.value), 'flex', env), `${w}: the card does not fill its cell`).toBe('1 1 auto')
      }
    }
  })
})

describe('QA-069: an unread room never leaves one word on its last line', () => {
  it('keeps the pool count and "could not be read" together', () => {
    const el = picker([
      ['a', profile({ pools: ['p1', 'p2', 'p3', 'p4', 'p5', 'p6', 'p7'], admission: admission({ headroom: null, basis: 'unknown', complete: false, unread: ['p1', 'p2', 'p3', 'p4', 'p5', 'p6', 'p7'] }) })],
    ])
    const room = card(el, 'a').querySelector('.sbf-runner-room')!
    // QA-069 changed only where it may break; the separator is a colon since run 3 (work-b-006).
    expect(visible(room)).toBe('not read room unknown: 7 of 7 pools could not be read')
    // MUTATION: drop the `sb-room-unread` span -> nothing holds the clause together.
    const clause = room.querySelector('.sb-room-unread')
    expect(clause, 'the count clause is not one unit').not.toBeNull()
    expect(visible(clause)).toBe('7 of 7 pools could not be read')
    for (const [w, env] of ENVS) {
      // MUTATION: drop `white-space: nowrap` from `.sb-room-unread` -> "be read" alone on the last line.
      expect(painted(clause!, 'white-space', env), `${w}: the clause can break inside itself`).toBe('nowrap')
    }
  })
})

describe('QA-105: a refused runner gives one reason in sight', () => {
  it('draws the platform\'s refusal and not the key a refused runner would need', async () => {
    const el = picker([
      ['claude-code', profile()],
      ['codex', profile({ provider: 'openai', available: false, disabled_reason: 'codex is disabled on this platform. Use claude-code.' })],
    ])
    const codex = card(el, 'codex')
    expect(codex.querySelector('input')!.disabled).toBe(true)
    expect(visible(codex.querySelector('.sbf-runner-off'))).toBe('Not enabled yet. Use claude-code')
    // MUTATION: draw `KeyFact` on a refused card again -> "openai key missing" beside the refusal.
    expect(visible(codex), 'a refused card names a key as its trouble').not.toMatch(/key (missing|present)/)
    expect(codex.querySelector('.sbf-runner-key'), 'a refused card draws a key fact').toBeNull()
    // Its size is still said: what it would take, had the platform offered it.
    expect(visible(codex.querySelector('.sbf-runner-facts'))).toBe('standard · 1 unit · Cloud Run')
    // A usable runner still names its key.
    await waitFor(() => expect(visible(card(el, 'claude-code').querySelector('.sbf-runner-key'))).toBe('anthropic key present'))
  })
})
