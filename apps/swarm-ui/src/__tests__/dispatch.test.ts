// The dispatch vocabulary, as behaviour rather than as source text.
//
// `dispatchOf` is the one place in this client where "the API did not say" and
// "the caller chose the default" have to stay apart, and they look identical
// once either is rendered. The Python source-grep that used to cover this
// asserted the string `dispatchOf` appeared in a screen -- which is true of a
// screen that calls it and throws the answer away.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { createElement } from 'react'
import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'

import { DispatchChoice, DispatchFacts, REPOSITORY_SCHEMES, repositorySchemeRefused } from '../Dispatch'
import * as TYPES from '../types'
import {
  CARRIER_NOTE,
  DEFAULT_CARRIER,
  DEFAULT_STRATEGY,
  DISPATCH_CARRIERS,
  DISPATCH_STRATEGIES,
  STRATEGY_LABEL,
  consequenceOf,
  dispatchOf,
  needsRepository,
  type DispatchStrategy,
  type Task,
  type TaskState,
} from '../types'

function task(dispatch: unknown): Task {
  return { id: 't1', dispatch } as unknown as Task
}

describe('dispatchOf', () => {
  it('reads back what the API sent', () => {
    const d = dispatchOf(task({ strategy: 'integrate', carrier: 'branches', role: 'integrator', integrates: ['t0'] }))
    expect(d).toEqual({ strategy: 'integrate', carrier: 'branches', role: 'integrator', integrates: ['t0'] })
  })

  it('an API that sent NO dispatch key at all reports null, never "collect"', () => {
    // THE DISTINCTION. `dispatch_of` on the server already substitutes
    // collect/checkpoints for a task that predates the feature, and that
    // substitution is correct. Substituting it a SECOND time here would invent
    // a caller's choice out of a version skew: "nothing was pushed because you
    // asked for collect" is a different sentence from "nothing was pushed and
    // we cannot say why".
    expect(dispatchOf(task(undefined))).toBeNull()
    expect(dispatchOf(task(null))).toBeNull()
  })

  it('a vocabulary neither side knows reports null rather than being coerced', () => {
    // `patches` is the WORKER's spelling and swarm-api refuses it outright. A
    // client that quietly rendered it as a carrier would be describing a
    // submission the API would have 422'd.
    expect(dispatchOf(task({ strategy: 'collect', carrier: 'patches', role: null, integrates: [] }))).toBeNull()
    expect(dispatchOf(task({ strategy: 'squash', carrier: 'checkpoints', role: null, integrates: [] }))).toBeNull()
  })

  it('an array or a scalar in the dispatch slot is not an object to read', () => {
    expect(dispatchOf(task([]))).toBeNull()
    expect(dispatchOf(task('collect'))).toBeNull()
  })

  it('an unknown role degrades to null without losing the rest', () => {
    const d = dispatchOf(task({ strategy: 'collect', carrier: 'checkpoints', role: 'reviewer', integrates: [] }))
    expect(d?.role).toBeNull()
    expect(d?.strategy).toBe('collect')
  })

  it('drops non-string entries from integrates rather than rendering them', () => {
    const d = dispatchOf(task({ strategy: 'integrate', carrier: 'checkpoints', role: 'integrator', integrates: ['t0', 7, null] }))
    expect(d?.integrates).toEqual(['t0'])
  })

  it('a missing integrates list is empty, not undefined', () => {
    const d = dispatchOf(task({ strategy: 'collect', carrier: 'checkpoints', role: null }))
    expect(d?.integrates).toEqual([])
  })
})

describe('the defaults are the API’s defaults', () => {
  it('and they are members of the offered vocabulary', () => {
    expect(DISPATCH_STRATEGIES).toContain(DEFAULT_STRATEGY)
    expect(DISPATCH_CARRIERS).toContain(DEFAULT_CARRIER)
    expect(DEFAULT_STRATEGY).toBe('collect')
    expect(DEFAULT_CARRIER).toBe('checkpoints')
  })

  it('every offered strategy has a label', () => {
    for (const s of DISPATCH_STRATEGIES) expect(STRATEGY_LABEL[s].trim()).not.toBe('')
  })

  it('the default option never promises a pull request', () => {
    // `collect` publishes nothing. A picker whose default reads like it opens
    // a PR sets an expectation the platform then fails.
    expect(STRATEGY_LABEL[DEFAULT_STRATEGY].toLowerCase()).not.toContain('pr')
    expect(STRATEGY_LABEL[DEFAULT_STRATEGY].toLowerCase()).not.toContain('pull request')
  })
})

describe('needsRepository', () => {
  it.each([
    ['collect', 'checkpoints', false],
    ['collect', 'branches', true],
    ['direct-pr', 'checkpoints', true],
    ['integrate', 'checkpoints', true],
  ] as ReadonlyArray<[DispatchStrategy, 'checkpoints' | 'branches', boolean]>)(
    '%s + %s -> %s',
    (strategy, carrier, expected) => {
      expect(needsRepository(strategy, carrier)).toBe(expected)
    },
  )
})

describe('consequenceOf', () => {
  it('integrate over six steps is ONE pull request and direct-pr is six', () => {
    // The whole reason the control exists: without this a caller is picking
    // between three words.
    expect(consequenceOf('integrate', 6).pullRequests).toBe(1)
    expect(consequenceOf('direct-pr', 6).pullRequests).toBe(6)
    expect(consequenceOf('collect', 6).pullRequests).toBe(0)
  })

  it('a count that can be zero is labelled a ceiling, and one that cannot is not', () => {
    // A step whose agent changed nothing opens nothing, so the PR figures are
    // an upper bound. `collect` opens none, always -- which is not a ceiling.
    expect(consequenceOf('direct-pr', 6).atMost).toBe(true)
    expect(consequenceOf('integrate', 6).atMost).toBe(true)
    expect(consequenceOf('collect', 6).atMost).toBe(false)
  })

  it('says plainly that collect pushes nothing', () => {
    const c = consequenceOf('collect', 3)
    expect(c.pushes).toBe(false)
    expect(c.headline).toContain('Nothing is pushed')
    expect(c.detail).toContain('read-only token')
  })

  it('integrate on a single step says there is nothing to integrate', () => {
    expect(consequenceOf('integrate', 1).detail).toContain('nothing to integrate')
    expect(consequenceOf('integrate', 2).detail).not.toContain('nothing to integrate')
  })

  it('never renders a zero step count or a broken plural', () => {
    for (const s of DISPATCH_STRATEGIES) {
      for (const n of [0, 1, 2, 7]) {
        const c = consequenceOf(s, n)
        expect(c.headline).not.toContain('0 step')
        expect(c.headline).not.toContain('1 steps')
        expect(c.detail).not.toContain('1 steps')
        expect(c.detail).not.toContain('undefined')
        expect(c.detail).not.toContain('NaN')
      }
    }
  })
})

describe('the carrier note', () => {
  it('says the carrier is recorded and not yet acted on', () => {
    // `_dispatch_carrier` in agent-worker has no caller in production code.
    // Describing a mechanism that is not wired up is precisely the claim this
    // client exists to not make.
    expect(CARRIER_NOTE).toContain('no worker code reads it yet')
    expect(CARRIER_NOTE).toContain('repository URL required')
  })
})

// ---------------------------------------------------------------------------
// The visual QA pass of 2026-09-25 (epics #82 and #84), rendered
// ---------------------------------------------------------------------------
//
// `createElement` rather than JSX: this file is `.ts`, and what it holds is
// the dispatch vocabulary's behaviour -- which now includes what the two
// dispatch components draw from it. Each case was committed RED first.

const COLLECT = { strategy: 'collect', carrier: 'checkpoints', role: null, integrates: [] }

function run(state: TaskState, dispatch: unknown, result_summary: Record<string, unknown> | null = null): Task {
  return { id: 't1', state, dispatch, result_summary } as unknown as Task
}

function facts(t: Task) {
  return render(createElement(DispatchFacts, { task: t }))
}

/** A `.ctl-fact`'s value, by its key; null when the strip has no such fact. */
function factOf(root: HTMLElement, key: string): string | null {
  const li = [...root.querySelectorAll('li.ctl-fact')].find((el) => el.querySelector('b')?.textContent === key)
  return li === undefined ? null : (li.textContent ?? '').slice(key.length)
}

describe('DispatchFacts, the read-back', () => {
  it('AG-25: claims no harvest for a run that has not harvested anything', () => {
    // QUEUED, READY and RUNNING have not finished; CANCELLED with no git block
    // never ran. Every one of them said "The patch was harvested into this
    // task's artifacts".
    const cases: Array<[TaskState, Record<string, unknown> | null]> = [
      ['QUEUED', null],
      ['READY', null],
      ['RUNNING', null],
      ['CANCELLED', {}],
    ]
    for (const [state, summary] of cases) {
      const { container, unmount } = facts(run(state, COLLECT, summary))
      expect(container.textContent, `${state}: a past-tense claim about a run that did not happen`).not.toMatch(
        /harvested|artifacts/,
      )
      // The strategy itself is still stated.
      expect(factOf(container, 'strategy'), state).toBe('collect')
      unmount()
    }
  })

  it('AG-25: says what a finished collect run published once its git block says it harvested', () => {
    const { container } = facts(run('SUCCEEDED', COLLECT, { git: { patch: 'changes.patch' } }))
    expect(factOf(container, 'published')).toBe('nothing, by request · patch in artifacts')
    // A harvest that produced no patch does not claim one.
    const empty = facts(run('SUCCEEDED', COLLECT, { git: { patch: null } }))
    expect(factOf(empty.container, 'published')).toBe('nothing, by request')
  })

  it('AG-24: is a facts strip, and the carrier keeps its "not acted on" mark with the note as its name', () => {
    const { container } = facts(
      run('RUNNING', { strategy: 'integrate', carrier: 'branches', role: 'integrator', integrates: ['t_a', 't_b'] }),
    )
    expect(container.querySelector('dl'), 'still prose in a definition list').toBeNull()
    const keys = [...container.querySelectorAll('ul.ctl-facts > li.ctl-fact > b')].map((b) => b.textContent)
    expect(keys).toEqual(['strategy', 'carrier', 'role', 'integrates'])
    const mark = container.querySelector('li.ctl-fact .ctl-mark')!
    expect(mark.textContent).toBe('not acted on')
    expect(mark.getAttribute('aria-label')).toBe(CARRIER_NOTE)
    // The note is the mark's NAME, not ink: no internal sentence on the surface.
    expect(container.textContent).not.toContain('no worker code reads it yet')
    // The order the patches are applied in is still on the surface.
    expect(factOf(container, 'integrates')).toBe('t_a → t_b')
  })
})

describe('DispatchChoice, the control', () => {
  function choice(repositoryUrl: string) {
    return render(
      createElement(DispatchChoice, {
        draft: { strategy: 'collect', carrier: 'checkpoints', repositoryUrl },
        onChange: () => {},
        steps: 1,
        scale: 'task',
      }),
    )
  }
  const schemeAlert = (root: HTMLElement) =>
    [...root.querySelectorAll('[role="alert"]')].find((a) => /https:\/\//.test(a.textContent ?? ''))

  it('TS-17: warns, without blocking, on a repository URL the API refuses on its scheme', () => {
    // `collect` + `checkpoints` needs no repository -- and a repository that IS
    // given is still validated, so `not a url` under the default is refused on
    // the click. The field accepted it without a word.
    const bad = choice('not a url')
    const alert = schemeAlert(bad.container)
    expect(alert, 'a URL the API will refuse gets no warning').toBeTruthy()
    expect(alert!.textContent).toContain('git@')
    expect(alert!.textContent).toContain('ssh://')
    // A WARNING: the field is not `type="url"`, which the browser would enforce
    // on submit -- and which would refuse `git@`, a form the API accepts.
    expect(bad.container.querySelector<HTMLInputElement>('#dsp-repo')!.type).toBe('text')
    bad.unmount()
    for (const fine of ['https://github.com/o/r.git', 'ssh://git@github.com/o/r.git', 'git@github.com:o/r.git', '', '   ']) {
      const r = choice(fine)
      expect(schemeAlert(r.container), JSON.stringify(fine)).toBeUndefined()
      r.unmount()
    }
  })

  it('TS-17: checks the URL as the form will SEND it, trimmed, against the API’s own prefixes', () => {
    expect(repositorySchemeRefused('  https://github.com/o/r.git  ')).toBe(false)
    expect(repositorySchemeRefused('http://github.com/o/r.git')).toBe(true)
    expect(repositorySchemeRefused('github.com/o/r')).toBe(true)
    // Blank is omitted from the request, so the scheme check never sees it.
    expect(repositorySchemeRefused('')).toBe(false)
    expect(repositorySchemeRefused(' \t ')).toBe(false)
  })

  it('TS-17: holds its prefixes to the validator in swarm_api/schemas.py, both copies of it', () => {
    // THE SECOND COPY IS HELD TO THE FIRST. `_repo_scheme` is stated on
    // TaskCreate and again on WorkflowCreate; the form's warning restates it.
    // A prefix added there and not here would warn about a URL the API
    // accepts -- a wrong caution, never a block, but a wrong one.
    const schemas = readFileSync(
      join(__dirname, '..', '..', '..', 'swarm-api', 'swarm_api', 'schemas.py'),
      'utf8',
    )
    const tuples = [...schemas.matchAll(/value\.startswith\(\(([^)]*)\)\)/g)].map((m) =>
      [...(m[1] ?? '').matchAll(/"([^"]*)"/g)].map((q) => q[1]).sort(),
    )
    expect(tuples.length, 'no `_repo_scheme` prefix tuple found in schemas.py; this compared nothing').toBeGreaterThanOrEqual(2)
    for (const t of tuples) expect(t).toEqual([...REPOSITORY_SCHEMES].sort())
  })

  it('TS-21: spaces its fields from the sheet, with no inline margin', () => {
    const { container } = choice('')
    const labels = [...container.querySelectorAll('label.t-label')]
    expect(labels.length).toBeGreaterThan(0)
    for (const label of labels) expect(label.getAttribute('style'), label.textContent ?? '').toBeNull()
  })
})

// ---------------------------------------------------------------------------
// The owner's decisions on epic #84, 2026-09-25 (TS-6, TS-14, TS-20)
// ---------------------------------------------------------------------------
//
// Each case was committed RED against the code it describes before the change
// landed.

function control(
  scale: 'task' | 'workflow',
  strategy: DispatchStrategy = 'collect',
  steps = scale === 'task' ? 1 : 3,
) {
  return render(
    createElement(DispatchChoice, {
      draft: { strategy, carrier: 'checkpoints', repositoryUrl: '' },
      onChange: () => {},
      steps,
      scale,
      terminals: scale === 'workflow' ? ['report'] : undefined,
    }),
  )
}
const radios = (root: HTMLElement) =>
  [...root.querySelectorAll<HTMLInputElement>('input[name="dispatch-strategy"]')].map((r) => r.value)

describe('TS-14: the task form offers what a task can be, and says where the rest lives', () => {
  it('draws only collect and direct-pr as choices at task scale, and no unavailable card', () => {
    const { container } = control('task')
    expect(radios(container), 'integrate is still drawn on the single-task form').toEqual(['collect', 'direct-pr'])
    // The dashed, faded card is the failed-read vocabulary (§8.6); it is gone,
    // not restyled.
    expect(container.querySelector('.dsp-option.is-off')).toBeNull()
    expect(container.querySelector('.dsp-off-why')).toBeNull()
    expect(container.textContent).not.toContain('Not available for a single task')
  })

  it('points to the workflow form in one plain line, with no mark and no warning', () => {
    const { container } = control('task')
    const link = container.querySelector<HTMLAnchorElement>('.dsp a[href="#work/new-workflow"]')
    expect(link, 'nothing says where one PR for several steps lives').not.toBeNull()
    expect(link!.textContent).toBe('Submit a workflow')
    const note = link!.closest('p')!
    expect(note.classList.contains('ctl-panel-note')).toBe(true)
    expect((note.textContent ?? '').replace(/\s+/g, ' ').trim()).toBe('one PR for several steps → Submit a workflow')
    expect(note.querySelector('.ctl-mark')).toBeNull()
    expect(note.classList.contains('warn-text')).toBe(false)
  })

  it('keeps all three strategies, all selectable, on the workflow form', () => {
    const { container } = control('workflow')
    expect(radios(container)).toEqual(['collect', 'direct-pr', 'integrate'])
    const disabled = [...container.querySelectorAll<HTMLInputElement>('input[name="dispatch-strategy"]')].filter((r) => r.disabled)
    expect(disabled).toEqual([])
    expect(container.querySelector('a[href="#work/new-workflow"]')).toBeNull()
  })
})

describe('TS-6: the carrier is a workflow question, asked once, and its caveat is one plain line', () => {
  it('is not asked on the single-task form, where there are no steps to carry work between', () => {
    const { container } = control('task')
    expect(container.querySelector('#dsp-carrier'), 'the carrier select is on the single-task form').toBeNull()
    expect(container.textContent).not.toContain('what carries work between steps')
    expect(container.textContent).not.toContain('not acted on')
  })

  it('names each carrier once on the workflow form', () => {
    const { container } = control('workflow')
    const options = [...container.querySelectorAll('#dsp-carrier option')].map((o) => o.textContent)
    expect(options, '"checkpoints — Checkpoints"').toEqual(['checkpoints', 'branches'])
  })

  it('says "not acted on yet" in plain ink with a help link, and keeps the full sentence as its name', () => {
    const { container } = control('workflow')
    // The two-sentence --warn paragraph is gone from the surface...
    expect(container.textContent).not.toContain('no worker code reads it yet')
    const warned = [...container.querySelectorAll('.warn-text')].map((w) => w.textContent ?? '')
    expect(warned.filter((w) => /worker|acted on/.test(w))).toEqual([])
    // ...and the fact stays on it, as one line with the way to the argument.
    const note = [...container.querySelectorAll('.ctl-panel-note')].find((p) => (p.textContent ?? '').includes('not acted on yet'))
    expect(note, 'the "not acted on" fact left the surface').toBeTruthy()
    expect(note!.querySelector('a[href="#help/dispatch-carrier"]')).not.toBeNull()
    expect(note!.getAttribute('aria-label')).toBe(CARRIER_NOTE)
    expect(container.querySelector('#dsp-carrier')!.getAttribute('aria-describedby')).toBe(note!.id)
  })
})

describe('TS-20: the control says each thing once', () => {
  it('draws no consequence box on the task form: the count on each option is the consequence', () => {
    for (const strategy of ['collect', 'direct-pr'] as const) {
      const { container, unmount } = control('task', strategy)
      expect(container.querySelector('.dsp-consequence'), `${strategy}: the box repeats the option`).toBeNull()
      unmount()
    }
  })

  it('draws the box on a workflow only to name the step that opens the one PR, without the headline again', () => {
    const { container } = control('workflow', 'integrate')
    const box = container.querySelector('.dsp-consequence')
    expect(box, 'the integrator preview is gone').not.toBeNull()
    expect(box!.getAttribute('role')).toBe('status')
    expect(box!.textContent).toContain('report')
    expect(container.querySelector('.dsp-consequence-head')).toBeNull()
    const headline = consequenceOf('integrate', 3).headline
    expect((container.textContent ?? '').split(headline).length - 1, 'the headline is printed twice').toBe(1)
  })

  it('draws nothing under the options for a workflow strategy the options already explain', () => {
    for (const strategy of ['collect', 'direct-pr'] as const) {
      const { container, unmount } = control('workflow', strategy)
      expect(container.querySelector('.dsp-consequence'), strategy).toBeNull()
      unmount()
    }
  })

  it('has no second label table for the carriers: the option is the value', () => {
    expect(Object.keys(TYPES), 'CARRIER_LABEL outlived its only caller').not.toContain('CARRIER_LABEL')
  })
})

// ---------------------------------------------------------------------------
// WF-13 (epic #83): the board card's own short value
// ---------------------------------------------------------------------------
//
// Every open workflow card carried the two-sentence form headline after its
// `opens` key -- "opens No pull request. Nothing is pushed." -- a capital
// letter and two full stops in the middle of a facts strip. The owner's
// decision: the card gets its own lowercase phrase, built in the same switch,
// and the forms (Dispatch's option count and SubmitWorkflow's outcome fact)
// keep the full sentence, which the cases above still pin. The Consequence box
// no longer repeats it under the option that already says it (TS-20, pinned
// in the TS-20 block above).

/** `opens` off the consequence. (It was read through a cast in the commit that
 *  put these cases in red, before `DispatchConsequence` carried the field.) */
const opensOf = (s: DispatchStrategy, n: number): string | undefined => consequenceOf(s, n).opens

describe('WF-13: consequenceOf(...).opens, the card value', () => {
  it('reads after the card’s "opens" key: lowercase, and no full stop', () => {
    let visited = 0
    for (const s of DISPATCH_STRATEGIES) {
      for (const n of [1, 2, 7]) {
        const opens = opensOf(s, n)
        visited += 1
        expect(typeof opens, `${s} × ${n} has no card value`).toBe('string')
        expect(opens!.charAt(0), `${s} × ${n}: "${opens}"`).toBe(opens!.charAt(0).toLowerCase())
        expect(opens, `${s} × ${n}: "${opens}"`).not.toContain('.')
        // The form's sentence is untouched: the card value is a second field,
        // not a rewrite of the first.
        const headline = consequenceOf(s, n).headline
        expect(headline.charAt(0)).toBe(headline.charAt(0).toUpperCase())
      }
    }
    expect(visited).toBe(DISPATCH_STRATEGIES.length * 3)
  })

  it('never claims more than the count does: a ceiling reads "up to"', () => {
    for (const s of DISPATCH_STRATEGIES) {
      for (const n of [1, 2, 7]) {
        if (consequenceOf(s, n).atMost) expect(opensOf(s, n), `${s} × ${n}`).toMatch(/^up to /)
        else expect(opensOf(s, n), `${s} × ${n}`).not.toMatch(/^up to /)
      }
    }
  })

  it('names the real step count for direct-pr, and says collect publishes nothing', () => {
    expect(opensOf('direct-pr', 7)).toContain('7 pull requests')
    expect(opensOf('direct-pr', 1)).toContain('1 pull request')
    expect(opensOf('direct-pr', 1)).not.toContain('1 pull requests')
    expect(opensOf('collect', 3)).toBe('no pull request and pushes nothing')
    expect(opensOf('integrate', 6)).toBe('up to 1 pull request, for all steps')
  })
})
