/**
 * QA ON SWARMCLOUD, LANE C6P (2026-10-07): the Submit forms.
 *
 *   G4-26  steps 2-3 of the task form were dimmed whole (opacity .55 over the
 *          labels' own muted ink), so "how this work gets merged" read at
 *          about 2.3:1. Only the controls are dimmed now; every label keeps
 *          its token at full strength (>= 4.5:1, both themes).
 *   G4-28  the carrier was a live select for a value the platform ignores. It
 *          is a disabled select reading "checkpoints (fixed)".
 *   G4-30  a refused runner read "Not enabled yet. Use claude-code" on a card
 *          and "not enabled yet. use claude-code" in a workflow option, and
 *          the issue form called runners that can start now "unavailable".
 *          One reason line for both shapes; "Other runners (N, not used for
 *          issue runs)" on the issue form.
 *   G4-31  the dispatch control's validation lines opened lowercase
 *          ("strategy direct-pr ends in…"). Sentence case.
 *   G4-33  the task picker offered the platform's own runners (indexer,
 *          merge, post-verdict, mock) beside claude-code, unmarked. They are
 *          marked "platform" and listed after the runners a caller submits.
 *   G4-34  the issue preview said "read 21:29:44" -- local, no date, no zone.
 *          It says "read just now", with the absolute UTC instant as its title.
 *   G4-35  the workflow form's two radio groups were two card styles. "If a
 *          step fails" is drawn as the dispatch control's card.
 *
 * MUTATIONS: put `opacity` back on `.sb-step.is-dim`; drop `disabled` from the
 * carrier select; `.toLowerCase()` the option reason again; restore
 * "unavailable" in the issue form's summary; lowercase "Strategy"; drop the
 * platform mark or the ordering; print `clockTime` again; draw `sb-option`
 * again -- each turns a case below red.
 */
import { render } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, describe, expect, it } from 'vitest'

import { DispatchChoice } from '../Dispatch'
import { IssuePreviewCard } from '../IssueSubmit'
import { RunnerPicker, RunnerSelect, isPlatformRunner } from '../RunnerPicker'
import { OnFailureChoice } from '../SubmitWorkflow'
import type { RunnerProfile } from '../types'
import type { CascadeEnv } from './cssgate'
import { THEMES, painted, resolveColour } from './marks'
import { contrast, over } from './spaceprobe'

const WIDE: CascadeEnv = { width: 1440 }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

function profile(extra: Partial<RunnerProfile> = {}): RunnerProfile {
  return { resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: null, units: 1, pools: [], ...extra }
}

const CODEX_REASON = 'codex is disabled on this platform (owner decision 2026-09-23). Use claude-code.'

// ---------------------------------------------------------------------------
// G4-26
// ---------------------------------------------------------------------------

describe('G4-26: a step not reached yet dims its controls, never its labels', () => {
  /** A dimmed step as Move and DispatchChoice draw it. */
  function dimStep(): HTMLElement {
    const host = document.createElement('div')
    host.innerHTML =
      '<form class="sbf"><div class="sbf-build"><section class="sbf-move sb-step is-dim">' +
      '<h2 class="sbf-move-h"><i class="sbf-move-n">3</i>Choose what happens to the result</h2>' +
      '<p class="sbf-none">no runner chosen</p>' +
      '<fieldset class="dsp"><legend class="t-label">How this work gets merged</legend>' +
      '<div class="dsp-options"><label class="dsp-option"><input type="radio" name="s" />' +
      '<span class="dsp-option-body"><span class="dsp-option-head"><b>Keep the patch</b></span>' +
      '<span class="dsp-count is-none">no pull request</span></span></label></div>' +
      '<label class="t-label dsp-label" for="r">Repository URL (optional)</label>' +
      '<input id="r" class="mono dsp-repo" /><select><option>x</option></select><textarea></textarea>' +
      '</fieldset></section></div></form>'
    document.body.appendChild(host)
    hosts.push(host)
    return host
  }

  const opacityOf = (el: Element): number => {
    let o = 1
    for (let n: Element | null = el; n !== null; n = n.parentElement) {
      const v = painted(n, 'opacity', WIDE)
      if (v !== null) o *= Number(v)
    }
    return o
  }
  const inkOf = (el: Element): string => {
    for (let n: Element | null = el; n !== null; n = n.parentElement) {
      const v = painted(n, 'color', WIDE)
      if (v !== null) return v
    }
    throw new Error('no colour above the element')
  }

  it('keeps every label at full strength, at least 4.5:1 on the card in both themes', () => {
    const host = dimStep()
    const words = ['.sbf-move-h', '.sbf-none', 'legend', '.dsp-label', '.dsp-option-head b', '.dsp-count']
    for (const sel of words) {
      const el = host.querySelector(sel)!
      expect(opacityOf(el), `${sel} is faded with its step`).toBe(1)
      for (const theme of THEMES) {
        const card = resolveColour('var(--surface)', theme)
        const ink = resolveColour(inkOf(el), theme)
        expect(contrast(over(ink, card), card), `${sel} in ${theme}`).toBeGreaterThanOrEqual(4.5)
      }
    }
  })

  it('dims the controls themselves', () => {
    const host = dimStep()
    for (const sel of ['input[type="radio"]', '.dsp-repo', 'select', 'textarea']) {
      expect(opacityOf(host.querySelector(sel)!), `${sel} is not dimmed`).toBeLessThan(1)
    }
  })
})

// ---------------------------------------------------------------------------
// G4-28 and G4-31: the dispatch control
// ---------------------------------------------------------------------------

function dispatch(scale: 'task' | 'workflow', strategy: 'collect' | 'direct-pr', carrier: 'checkpoints' | 'branches' = 'checkpoints') {
  return render(
    createElement(DispatchChoice, {
      draft: { strategy, carrier, repositoryUrl: '' },
      onChange: () => {},
      steps: scale === 'task' ? 1 : 3,
      scale,
      terminals: scale === 'workflow' ? ['report'] : undefined,
    }),
  )
}

describe('G4-28: the carrier is shown fixed, not offered', () => {
  it('is a disabled select reading "checkpoints (fixed)"', () => {
    const { container } = dispatch('workflow', 'collect')
    const select = container.querySelector<HTMLSelectElement>('#dsp-carrier')!
    expect(select.disabled, 'the carrier is a live control').toBe(true)
    expect([...select.options].map((o) => o.textContent)).toEqual(['checkpoints (fixed)'])
    expect(select.value).toBe('checkpoints')
  })
})

describe('G4-31: validation lines are sentences', () => {
  it('opens the repository warnings with a capital and ends them with a stop', () => {
    const cases = [dispatch('task', 'direct-pr'), dispatch('workflow', 'collect', 'branches')]
    for (const { container, unmount } of cases) {
      const warns = [...container.querySelectorAll('.warn-text')].map(visible)
      expect(warns.length, 'no warning drawn').toBeGreaterThan(0)
      for (const w of warns) expect(w, w).toMatch(/^[A-Z].*\.$/)
      unmount()
    }
  })
})

// ---------------------------------------------------------------------------
// G4-30 and G4-33: the runner lists
// ---------------------------------------------------------------------------

const CATALOGUE: Array<readonly [string, RunnerProfile]> = [
  ['claude-code', profile({ provider: 'anthropic' })],
  ['codex', profile({ provider: 'openai', available: false, disabled_reason: CODEX_REASON })],
  ['indexer', profile({ provider: 'anthropic' })],
  ['merge', profile({ provider: 'git' })],
  ['mock', profile()],
  ['post-verdict', profile({ provider: 'git-review' })],
]
const KEYS = { kind: 'read', registered: new Map<string, boolean>() } as const

describe('G4-30: one wording for a refused runner, and "unavailable" only when it is', () => {
  it('words a refused runner the same on a card and in a workflow option', () => {
    const card = render(<RunnerPicker group="g" label="runner" profiles={CATALOGUE} chosen="" keys={KEYS} onPick={() => {}} />)
    const off = card.container.querySelector('input[value="codex"]')!.closest('label')!.querySelector('.sbf-runner-off')!
    expect(visible(off)).toBe('Not enabled yet. Use claude-code')
    card.unmount()
    const sel = render(<RunnerSelect id="s" label="runner" profiles={CATALOGUE} chosen="" onPick={() => {}} />)
    const option = sel.container.querySelector('option[value="codex"]')!
    expect(option.textContent).toBe('codex · Not enabled yet. Use claude-code')
  })

  it('says the issue form holds its runners back because issue runs do not use them', () => {
    const held = CATALOGUE.map(([n, p]): readonly [string, RunnerProfile] =>
      n === 'claude-code' ? [n, p] : [n, { ...p, available: false, disabled_reason: 'Issue runs always use claude-code' }])
    const { container } = render(
      <RunnerPicker group="i" label="runner" profiles={held} chosen="claude-code" keys={KEYS} onPick={() => {}}
        onlyUsable heldAs="not used for issue runs" />,
    )
    expect(visible(container.querySelector('.sbf-runners-more > summary'))).toBe('Other runners (5, not used for issue runs)')
  })

  it('keeps "unavailable" where the platform refuses them', () => {
    const many: Array<readonly [string, RunnerProfile]> = [
      ['claude-code', profile()],
      ...['a', 'b', 'c'].map((n) => [n, profile({ available: false, disabled_reason: CODEX_REASON })] as const),
    ]
    const { container } = render(<RunnerPicker group="g" label="runner" profiles={many} chosen="" keys={KEYS} onPick={() => {}} />)
    expect(visible(container.querySelector('.sbf-runners-more > summary'))).toBe('Other runners (3 unavailable)')
  })
})

describe('G4-33: the platform\'s own runners are marked and listed last', () => {
  it('names which runners are the platform\'s', () => {
    expect(['indexer', 'merge', 'post-verdict', 'mock', 'claude-code-review'].filter((n) => !isPlatformRunner(n))).toEqual([])
    expect(['claude-code', 'codex', 'browser'].filter(isPlatformRunner)).toEqual([])
  })

  it('draws the runners a caller submits first, and marks the rest "platform"', () => {
    const { container } = render(<RunnerPicker group="g" label="runner" profiles={CATALOGUE} chosen="" keys={KEYS} onPick={() => {}} />)
    const cards = [...container.querySelectorAll('.sbf-runner')]
    const names = cards.map((c) => visible(c.querySelector('.sbf-runner-name')))
    expect(names).toEqual(['claude-code', 'codex', 'indexer', 'merge', 'mock', 'post-verdict'])
    const marked = cards.filter((c) => visible(c.querySelector('.sb-runner-platform')) === 'platform')
      .map((c) => visible(c.querySelector('.sbf-runner-name')))
    expect(marked).toEqual(['indexer', 'merge', 'mock', 'post-verdict'])
  })
})

// ---------------------------------------------------------------------------
// G4-34
// ---------------------------------------------------------------------------

describe('G4-34: the issue preview says how long ago it was read', () => {
  it('reads "just now", with the absolute UTC instant as the title', () => {
    const at = Date.now()
    const read = {
      issue: {
        ref: 'o/r#1', owner: 'o', repo: 'r', number: 1, url: 'https://github.com/o/r/issues/1', repository_url: 'https://github.com/o/r',
        title: 'T', body: '', body_truncated: false, body_redacted: false, labels: [], state: 'open', comments: 0,
      },
      tenant_id: 'eng',
    }
    const { container } = render(<IssuePreviewCard read={read as never} at={at} closedOk={false} onPlanAnyway={() => {}} />)
    const line = container.querySelector('.in-read-at')!
    expect(visible(line)).toBe('read just now with this tenant’s forge credential')
    const iso = new Date(at).toISOString()
    expect(line.getAttribute('title')).toBe(`${iso.slice(0, 10)} ${iso.slice(11, 19)} UTC`)
  })
})

// ---------------------------------------------------------------------------
// G4-35
// ---------------------------------------------------------------------------

describe('G4-35: the workflow form draws one radio card', () => {
  it('draws "If a step fails" as the dispatch control\'s card', () => {
    const { container } = render(<OnFailureChoice value="fail_workflow" onChange={() => {}} />)
    const group = container.querySelector('[role="radiogroup"]')!
    expect(group.classList.contains('dsp-options')).toBe(true)
    const cards = [...group.querySelectorAll('label')]
    expect(cards.map((c) => c.className)).toEqual(['dsp-option is-on', 'dsp-option'])
    expect(cards.map((c) => visible(c.querySelector('.dsp-option-head b')))).toEqual(['Fail the workflow', 'Continue'])
    expect(container.querySelector('.sb-option')).toBeNull()
  })

  it('paints its selected card the way the strategy\'s is painted', () => {
    const { container } = render(
      <div><div className="dsp-options"><label className="dsp-option is-on" /></div>
        <OnFailureChoice value="fail_workflow" onChange={() => {}} /></div>,
    )
    const [strategy, failure] = [...container.querySelectorAll('label.is-on')]
    for (const prop of ['border-left-color', 'box-shadow', 'background', 'padding', 'border-radius']) {
      expect(painted(failure!, prop, WIDE), prop).toBe(painted(strategy!, prop, WIDE))
    }
  })
})
