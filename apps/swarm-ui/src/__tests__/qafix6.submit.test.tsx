/**
 * QAFIX6-SUBMIT, WORK › START AN AGENT (part of #1680, visual QA run 6,
 * wf_31aca00c977e4d37a935, 2026-10-23, the fixture build at 1440 and 390 px,
 * light and dark, and the boxes earlier runs left open):
 *
 *   a. 6:flow-submit-form-desktop-01/-02 (box 6283734173, QA-103): an empty
 *      submit named the missing runner only in the side panel. A submit
 *      attempt, by Enter or the button, now marks each missing input at its
 *      own field.
 *   b. boxes 6283733687 / 6283734601 / 6283735401 (QA-102, -104, -107): a
 *      setting row left wholly empty says it will not be sent; its value box
 *      has a name.
 *   c. box 6329552941: claude-code-browser says "waits for room" (verified:
 *      the unread-room wording is a question, see the PR).
 *   d. box 6329553459 and 6:work-b-005: a runner pick keeps the page where
 *      it was, and the docked "Not ready" strip hides while the summary
 *      card's own line is in view, so the reason is said once.
 *   e. box 6343781466: the three disabled primary actions look disabled.
 *   f. boxes 6343782161 / 6343782986: codex's disabled radio has an edge a
 *      reader can see (3:1), and hints, placeholders and the runner select
 *      are set in the sans face.
 *
 * Every assertion names the mutation that turns it red.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { InputFields, SubmitScreen, type InputField, type ValueKind } from '../Submit'
import { RunnerSelect } from '../RunnerPicker'
import type { CascadeEnv } from './cssgate'
import { THEMES, build, painted, resolveColour, sameColour, tokensIn } from './marks'
import { contrast } from './spaceprobe'

const WAIT = { timeout: 4000 }
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

const hosts: HTMLElement[] = []
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  for (const h of hosts.splice(0)) h.remove()
  document.body.innerHTML = ''
})

async function form() {
  const view = render(<SubmitScreen />)
  const button = await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
  return { container: view.container as HTMLElement, button: button as HTMLButtonElement }
}

function pick(container: HTMLElement, name: string) {
  const r = container.querySelector<HTMLInputElement>(`input[name="runner-profile"][value="${name}"]`)
  if (r === null) throw new Error(`no runner ${name} on the form`)
  fireEvent.click(r)
}

const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

let key = 7000
function f(over: Partial<InputField> = {}): InputField {
  return {
    key: key++, name: '', kind: 'text' as ValueKind, text: '', flag: false,
    list: [''], actions: [], required: false, own: false, ...over,
  }
}

// ---------------------------------------------------------------------------
// a. errors at the field
// ---------------------------------------------------------------------------

describe('a: a submit attempt marks each missing input at its own field', () => {
  it('an empty submit: the runner group is invalid and has its own error text', async () => {
    const { container } = await form()
    const group = screen.getByRole('radiogroup', { name: 'runner' })
    // Before any attempt the group is not an error.
    // MUTATION: mark the group invalid while `chosen === ''` alone -> an untouched form opens red.
    expect(group.getAttribute('aria-invalid')).toBeNull()
    fireEvent.submit(container.querySelector('form')!)
    // MUTATION: drop the picker's `invalid` -> the runner is named only in the side panel, as run 6 read it.
    expect(group.getAttribute('aria-invalid')).toBe('true')
    const said = group.getAttribute('aria-describedby')
    expect(said, 'the group is described by nothing').not.toBeNull()
    // MUTATION: describe the group by the panel's `sbf-why` -> the error text is not the field's own.
    const line = document.getElementById(said!)
    expect(line).not.toBeNull()
    expect(line!.closest('.sbf-send')).toBeNull()
    expect(visible(line)).toMatch(/runner/)
  })

  it('Enter in a field of the empty form is an attempt too, though the button is disabled', async () => {
    const { container, button } = await form()
    // The button stays disabled (item e): the attempt reaches the form some other way.
    expect(button.disabled).toBe(true)
    // MUTATION: drop the form's Enter handler -> a disabled default button swallows the attempt.
    fireEvent.keyDown(container.querySelector<HTMLInputElement>('#dsp-repo')!, { key: 'Enter' })
    expect(screen.getByRole('radiogroup', { name: 'runner' }).getAttribute('aria-invalid')).toBe('true')
  })

  it('a press on the disabled button is an attempt too', async () => {
    const { button } = await form()
    // A disabled button takes no pointer events (`.sbf-go`, submit.css); the
    // press lands on the strip that holds it.
    // MUTATION: drop the strip's click handler -> pressing Start on the empty form does nothing.
    fireEvent.click(button.parentElement!)
    expect(screen.getByRole('radiogroup', { name: 'runner' }).getAttribute('aria-invalid')).toBe('true')
    expect(painted(button, 'pointer-events', WIDE)).toBe('none')
  })

  it('a runner without its prompt: the prompt field is invalid after the attempt, not before', async () => {
    const { container } = await form()
    pick(container, 'claude-code')
    const field = container.querySelector<HTMLElement>('.sbf-field.is-required')!
    const editor = field.querySelector('textarea')!
    expect(editor.getAttribute('aria-invalid')).toBeNull()
    fireEvent.submit(container.querySelector('form')!)
    // MUTATION: keep `gap` on blur alone -> the attempt leaves the prompt unmarked.
    expect(editor.getAttribute('aria-invalid')).toBe('true')
    expect(field.querySelectorAll('.sbf-miss')).toHaveLength(1)
    // And the runner, now chosen, is no longer the error.
    expect(screen.getByRole('radiogroup', { name: 'runner' }).getAttribute('aria-invalid')).toBeNull()
  })

  it('an invalid runner group is painted as an error', () => {
    const card = build('ul.sbf-runners.is-cards > li > label.sbf-runner', hosts)
    card.closest('ul')!.setAttribute('aria-invalid', 'true')
    // MUTATION: drop the `[aria-invalid]` rule -> the group says "invalid" to a screen reader and nothing to the eye.
    expect(painted(card, 'border-color', WIDE)).toBe('var(--warn)')
  })
})

// ---------------------------------------------------------------------------
// b. custom setting rows
// ---------------------------------------------------------------------------

describe('b: a wholly empty setting row says it will not be sent', () => {
  it('flags the row, and its value box has a name', () => {
    const { container } = render(
      <InputFields profile="mock" required={[]} idPrefix="t" onChange={() => {}} fields={[f({ own: true })]} />,
    )
    // MUTATION: word the blank row as the nameless one -> a reader cannot tell an empty row from a named-less value.
    expect(visible(container.querySelector('.sbf-unsent'))).toBe('this empty row will not be sent')
    // MUTATION: drop the own row's `aria-label` -> the value is a bare "textbox" (QA-104).
    expect(screen.getByRole('textbox', { name: /value/ })).toBeTruthy()
  })
})

// ---------------------------------------------------------------------------
// c. 'waits for room' whenever room is unknown
// ---------------------------------------------------------------------------

describe('c: claude-code-browser, as run 6 drew it, says it waits for room', () => {
  // The fixture serves claude-code-browser a measured room of 0 since
  // QAFIX4-FIXTURE-B F10; an UNREAD room reads "may wait for room: room not
  // read" (N26, pinned exactly in qafix4.submit.test.tsx). Whether that
  // becomes "waits for room" is a question in this lane's PR, not a change.
  it('claude-code-browser on the fixture', async () => {
    const { container } = await form()
    pick(container, 'claude-code-browser')
    const panel = container.querySelector<HTMLElement>('.sbf-send')!
    const lands = [...panel.querySelectorAll('.ctl-fact')].find((li) => li.querySelector('b')?.textContent === 'lands as')!
    // MUTATION: drop the room-0 clause for this runner -> it reads differently from its neighbours at 0.
    expect(visible(lands)).toMatch(/waits for room/)
  })
})

// ---------------------------------------------------------------------------
// d. the phone form
// ---------------------------------------------------------------------------

class InView {
  constructor(private readonly cb: IntersectionObserverCallback) {}
  observe(el: Element) {
    queueMicrotask(() => this.cb([{ target: el, isIntersecting: true } as IntersectionObserverEntry], this as unknown as IntersectionObserver))
  }
  unobserve() {}
  disconnect() {}
  takeRecords(): IntersectionObserverEntry[] {
    return []
  }
}

describe('d: the phone form keeps its place and says "Not ready" once', () => {
  it('a runner pick does not put the scroller back at the top', async () => {
    const scroller = document.createElement('div')
    scroller.className = 'ctl-scroll'
    document.body.appendChild(scroller)
    hosts.push(scroller)
    let top = 600
    Object.defineProperty(scroller, 'scrollTop', { configurable: true, get: () => top, set: (v: number) => { top = v } })
    render(<SubmitScreen />, { container: scroller })
    await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
    pick(scroller, 'claude-code')
    // MUTATION: reset the scroller to 0 on a pick -> the runner's detail drops below the fold at 390.
    expect(top).not.toBe(0)
  })

  it('with the summary card\'s line in view, the docked strip is not drawn', async () => {
    vi.stubGlobal('IntersectionObserver', InView)
    const { container } = await form()
    expect(container.querySelector('.sbf-why')).not.toBeNull()
    // MUTATION: draw `.sbf-dock` whatever the card's line shows -> 390 says "Not ready: choose a runner." twice.
    await vi.waitFor(() => expect(container.querySelector('.sbf-dock')).toBeNull(), WAIT)
  })
})

// ---------------------------------------------------------------------------
// e. disabled primary actions look disabled
// ---------------------------------------------------------------------------

describe('e: a disabled primary action in a send panel is not drawn as a pale accent', () => {
  for (const theme of THEMES) {
    it(`${theme}: Start one agent, Submit this workflow, Plan this issue`, async () => {
      const { button } = await form()
      const built = build('div.sbf-send > button.c-btn.is-primary.is-full', hosts)
      built.setAttribute('disabled', '')
      for (const el of [button, built]) {
        const env: CascadeEnv = { ...WIDE, theme }
        const fill = painted(el, ['background', 'background-color'], env)!
        const accents = ['var(--sk-ac)', 'var(--sk-acs)'].map((v) => resolveColour(v, theme))
        // MUTATION: leave the components.css pale-accent fill -> disabled reads as a light-blue live button.
        for (const t of tokensIn(fill, theme)) {
          for (const a of accents) expect(sameColour(t.rgba, a), `${t.token} is an accent`).toBe(false)
        }
        expect(painted(el, 'color', env)).toBe('var(--text-dim)')
      }
      // MUTATION: drop `cursor: not-allowed` from the strip -> the pointer says nothing over a disabled Start.
      expect(painted(button.parentElement!, 'cursor', WIDE)).toBe('not-allowed')
      expect(painted(built, 'cursor', WIDE)).toBe('not-allowed')
    })
  }
})

// ---------------------------------------------------------------------------
// f. the codex radio and monospace hints
// ---------------------------------------------------------------------------

describe('f: the disabled radio has an edge, and hints are in the sans face', () => {
  for (const theme of THEMES) {
    it(`${theme}: the disabled runner radio measures at least 3:1 against its card`, () => {
      const card = build('div.sbf-runners.is-cards > label.sbf-runner.is-off', hosts)
      const radio = document.createElement('input')
      radio.type = 'radio'
      radio.disabled = true
      card.appendChild(radio)
      const env: CascadeEnv = { ...WIDE, theme }
      const edge = painted(radio, ['border', 'border-color'], env)!
      const ground = resolveColour('var(--surface)', theme)
      const [ink] = tokensIn(edge, theme)
      // MUTATION: put `var(--line)` back on the disabled radio -> 1.15:1, codex reads as a card with no control.
      expect(ink, 'the edge names no token').toBeDefined()
      expect(contrast(ink!.rgba, ground)).toBeGreaterThanOrEqual(3)
    })
  }

  it('hint lines, placeholders, the fixed carrier and the runner select are not monospace', () => {
    const SANS = 'var(--font)'
    const note = build('div.sbf > p.ctl-panel-note', hosts)
    const fixed = build('div.sbf > p.dsp-fixed', hosts)
    const input = build('div.sbf > input.mono', hosts)
    const area = build('div.sbf > textarea', hosts)
    for (const env of [WIDE, PHONE]) {
      // MUTATION: drop the `.sbf .ctl-panel-note` rule -> "one PR for several steps →" is in the code face.
      expect(painted(note, 'font-family', env)).toBe(SANS)
      // MUTATION: drop `.sbf .dsp-fixed` -> "checkpoints (fixed)" is in the code face.
      expect(painted(fixed, 'font-family', env)).toBe(SANS)
      // MUTATION: drop the `::placeholder` rule -> "setting name" and "one entry" are in the code face.
      expect(painted(input, 'font-family', env, '::placeholder')).toBe(SANS)
      expect(painted(area, 'font-family', env, '::placeholder')).toBe(SANS)
    }
    const { container } = render(
      <div className="app"><div className="sbf"><RunnerSelect id="s" label="runner" profiles={[]} chosen="" onPick={() => {}} /></div></div>,
    )
    // MUTATION: put `mono` back on the select -> "choose a runner" is in the code face.
    const select = container.querySelector('select')!
    expect(painted(select, 'font-family', WIDE)).not.toBe('var(--mono)')
  })
})
