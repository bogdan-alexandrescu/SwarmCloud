/**
 * CONSOLE VISUAL QA PASS 2, LANE QA2-FIX-27 (part of #2694 and #2391; BGD
 * 2026-10-28). One case per finding this lane changed, each red on main
 * before the change. jsdom has no layout, so each reads the cascade of the
 * sheets in the order the page loads them (`cssgate.ts`) on the real markup.
 *
 *   runs2-12   a PLANNED run's sticky action bar is the plan card's whole
 *              width: runs.css capped it at `max-width: 100%`, so its -14px
 *              margins shifted it and its top rule stopped 28px short;
 *   runs2-33   Start an agent's summary draws a value not chosen yet in the
 *              neutral muted ink, not the warm `--ctl-absent`;
 *   config2-17 Workspace Settings' head keeps no bottom margin over the
 *              page's 12px gap, so its tabs sit where GitHub access's do.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import { SubmitScreen } from '../Submit'
import { cascade, type CascadeEnv } from './cssgate'
import SHEET from '../styles.css?raw'
import INTAKE from '../styles/intake.css?raw'
import RUNS from '../styles/runs.css?raw'
import SUBMIT from '../styles/submit.css?raw'
import CONFIG from '../styles/config.css?raw'
import KEYS from '../styles/keys.css?raw'
import REPOSITORIES from '../styles/repositories.css?raw'
import WORKSPACE from '../styles/workspace-settings.css?raw'

const PHONE: CascadeEnv = { width: 390, theme: 'light' }
const DESK: CascadeEnv = { width: 1440 }
const WAIT = { timeout: 4000 }

const painted = (sheet: string, el: Element, property: string | readonly string[], env: CascadeEnv) =>
  cascade(sheet, el, property, env).winner

afterEach(() => cleanup())

describe('runs2-12: the plan foot\'s sticky bar spans the card', () => {
  // Runs.tsx draws a PLANNED run's plan unfolded, the bar the section's own
  // child (qa.u12.runs.test.tsx pins `.rn-plan .rn-actbar` on the real page).
  function bar(): Element {
    document.body.innerHTML = '<section class="rn-plan"><ol class="rn-steps"><li class="rn-step"></li></ol><div class="rn-actbar"></div></section>'
    return document.querySelector('.rn-actbar')!
  }

  it('is not capped at the track, so its -14px margins widen it to the card\'s edges', () => {
    const el = bar()
    // Runs.tsx loads intake.css, then runs.css.
    const sheet = `${SHEET}\n${INTAKE}\n${RUNS}`
    expect(painted(sheet, el, 'margin', PHONE)?.value).toBe('0 -14px -12px')
    // MUTATION: drop `.rn-plan > .rn-actbar { max-width: none }` (intake.css)
    // and runs.css's `.rn-plan > * { max-width: 100% }` wins: '100%'.
    expect(painted(sheet, el, 'max-width', PHONE)?.value).toBe('none')
  })

  it('stays opaque and draws a shade above its top rule', () => {
    const el = bar()
    const sheet = `${SHEET}\n${INTAKE}\n${RUNS}`
    expect(painted(sheet, el, 'background', PHONE)?.value).toBe('var(--surface)')
    expect(painted(sheet, el, 'border-top', PHONE)?.value).toBe('1px solid var(--line)')
    // MUTATION: drop the box-shadow and nothing marks the plan going on under it.
    expect(painted(sheet, el, 'box-shadow', PHONE)?.value ?? '').toMatch(/^0 -6px/)
  })
})

describe('runs2-33: a summary value not chosen yet is neutral, not amber', () => {
  it('draws runner, input, cost and room in the muted ink until a runner is picked', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
    // Submit.tsx imports submit.css after main.tsx's styles.css.
    const sheet = `${SHEET}\n${SUBMIT}`
    const rows = [...container.querySelectorAll('.sbf-send > .ctl-facts > .ctl-fact.is-absent')]
    const keys = rows.map((li) => li.querySelector('b')?.textContent)
    expect(keys).toEqual(['runner', 'input', 'cost', 'room'])
    for (const li of rows) {
      const dash = li.querySelector('.ctl-em')!
      expect(dash.textContent).toBe('—')
      for (const env of [PHONE, { width: 390, theme: 'dark' } as CascadeEnv]) {
        // MUTATION: drop the `form.sbf:not(.in-form)` rule (submit.css) and
        // both read `var(--ctl-absent)`, the warm hue of a value not read.
        expect(painted(sheet, li, 'color', env)?.value).toBe('var(--text-faint)')
        expect(painted(sheet, dash, 'color', env)?.value).toBe('var(--text-faint)')
      }
    }
  }, 20000)

  it('leaves the issue form\'s unread values in the absent hue', () => {
    document.body.innerHTML = '<form class="sbf in-form"><aside class="sbf-side"><div class="sbf-send in-send"><ul class="ctl-facts"><li class="ctl-fact is-absent"><b>workspace</b><i class="ctl-em">— not served</i></li></ul></div></aside></form>'
    const sheet = `${SHEET}\n${SUBMIT}`
    // MUTATION: drop `:not(.in-form)` and "not served" loses the hue that says it was not read.
    expect(painted(sheet, document.querySelector('.ctl-em')!, 'color', PHONE)?.value).toBe('var(--ctl-absent)')
  })
})

describe('config2-17: Workspace Settings\' tabs at the same y as the other Config pages\'', () => {
  // The Config pages' boxes as their screens draw them (WorkspaceSettings.tsx,
  // WorkspacePeople.tsx, Access.tsx), inside App.tsx's `.cfg-page`.
  function page(box: string): Element {
    document.body.innerHTML = `<div class="cfg-page"><div class="${box}"><div class="c-phead"><div class="head"><h1>Title</h1></div></div><div class="c-tabs cfg-parts"></div></div></div>`
    return document.querySelector('.c-phead')!
  }
  const sheet = `${SHEET}\n${KEYS}\n${REPOSITORIES}\n${CONFIG}\n${WORKSPACE}`
  const bottom = (el: Element, env: CascadeEnv) => {
    const w = painted(sheet, el, ['margin', 'margin-bottom'], env)
    if (w === null) return null
    return w.property === 'margin-bottom' ? w.value : w.value.split(/\s+/)[2] ?? w.value.split(/\s+/)[0]
  }

  it('keeps the head\'s bottom margin at 0, as GitHub access\'s, so the gap is the page\'s 12px alone', () => {
    // The control: GitHub access (`.ur-page`) is drawn at y 77.
    expect(bottom(page('ur-page ac-page'), DESK)).toBe('0')
    for (const env of [DESK, PHONE]) {
      // MUTATION: drop `.ws-page > .c-phead { margin-bottom: 0 }` and this is
      // `.c-phead`'s `var(--ctl-s3)`: the tabs 12px lower, at y 89.
      expect(bottom(page('pk-page ws-page'), env)).toBe('0')
      expect(bottom(page('pk-page wp-page'), env)).toBe('0')
    }
  })
})
