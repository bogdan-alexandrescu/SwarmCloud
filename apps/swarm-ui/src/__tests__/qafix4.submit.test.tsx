/**
 * QAFIX4-SUBMIT, THE SUBMIT FORMS (part of #1680, visual QA run 4,
 * wf_ecd935541ced4a539540, the fixture build of main at fabe4d6c, 2026-10-22):
 *
 *   N25 (run-4 work-b-005)  the issue form's disabled merge switch had no
 *           track in dark: `:disabled { opacity: .45 }` faded the hairline
 *           edge and the `--surface-2` fill to about 1.05:1 on the card, so
 *           only a grey knob showed beside "off".
 *   N26 (run-4 flow-submit-form-desktop-03)  "lands as" said "waits for
 *           room" for a room of 0 and nothing for a room that was not read.
 *   N27 (run-4 flow-submit-form-phone-390-02, -03)  at 390 the "Not ready"
 *           reason sat below every step, screens from its field, and a runner
 *           pick left the page somewhere other than the pick.
 *   c09 (box comment 6283735984, owner decision 2026-10-22)  with a measured
 *           room of 0 the note under "Start one agent" says the task is
 *           accepted and waits, in the warning ink.
 *
 * Every assertion names the mutation that turns it red.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SubmitScreen } from '../Submit'
import type { Capacity } from '../types'
import type { CascadeEnv } from './cssgate'
import { build, painted, resolveColour, THEMES } from './marks'
import { contrast, over } from './spaceprobe'

// THE UNREAD ROOM IS BUILT HERE, not found in the fixture. Since
// QAFIX4-FIXTURE-B F10 the fixture serves `claude-code-browser` the admission
// row the analyser produces (a measured room of 0), so no fixture profile is
// left unread. Every other read stays the development fixture's.
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    loadCapacity: async (...a: Parameters<typeof actual.loadCapacity>) => {
      const r = await actual.loadCapacity(...a)
      if (r.status !== 'ok') return r
      const profiles: Capacity['runner_profiles'] = {
        ...r.data.runner_profiles,
        'claude-code-browser': { ...r.data.runner_profiles['claude-code-browser']!, admission: undefined },
      }
      return { ...r, data: { ...r.data, runner_profiles: profiles } }
    },
  }
})

const WAIT = { timeout: 4000 }
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

const hosts: HTMLElement[] = []
afterEach(() => {
  vi.restoreAllMocks()
  for (const h of hosts.splice(0)) h.remove()
  document.body.innerHTML = ''
})

async function form(container?: HTMLElement) {
  const view = render(<SubmitScreen />, container === undefined ? {} : { container })
  await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
  return view.container as HTMLElement
}

function radio(container: HTMLElement, name: string): HTMLInputElement {
  const r = container.querySelector<HTMLInputElement>(`input[name="runner-profile"][value="${name}"]`)
  if (r === null) throw new Error(`no runner ${name} on the form`)
  return r
}

const landsAs = (container: HTMLElement) =>
  visible([...container.querySelectorAll('.sbf-send .ctl-fact')].find((li) => li.querySelector('b')?.textContent === 'lands as') ?? null)

// ---------------------------------------------------------------------------
// N25
// ---------------------------------------------------------------------------

/** The issue form's switch as IssueSubmit draws it: `.in-switch > input[role=switch]`. */
function mergeSwitch(disabled: boolean): HTMLInputElement {
  const host = build('span.in-switch', hosts)
  const input = document.createElement('input')
  input.type = 'checkbox'
  input.setAttribute('role', 'switch')
  input.disabled = disabled
  host.appendChild(input)
  return input
}

/** The colour a `border` / `border-color` value names. */
function edgeColour(value: string): string {
  const m = value.match(/var\(--[\w-]+\)|#[0-9a-f]{3,8}\b/gi)
  if (m === null) throw new Error(`no colour in ${JSON.stringify(value)}`)
  return m[m.length - 1]!
}

describe('N25: the disabled merge switch keeps an outline that reads as a switch', () => {
  it('draws its edge at 3:1 or more on the card in dark and in light, at full strength', () => {
    const input = mergeSwitch(true)
    const opacity = Number(painted(input, 'opacity', WIDE) ?? '1')
    // MUTATION: put `opacity: .45` back on `.in-switch > input:disabled` -> the whole control fades again.
    expect(opacity, 'the disabled switch is faded as a whole').toBe(1)
    const edge = painted(input, ['border', 'border-color'], WIDE)
    expect(edge, 'no edge on the switch').not.toBeNull()
    for (const theme of THEMES) {
      const card = resolveColour('var(--surface)', theme)
      const ink = resolveColour(edgeColour(edge!), theme)
      const seen = over({ ...ink, a: ink.a * opacity }, card)
      // MUTATION: leave the disabled edge on `--line` -> 1.3:1 on the card, the run-4 stray dot.
      expect(contrast(seen, card), `${theme}: the disabled track's edge on the card`).toBeGreaterThanOrEqual(3)
    }
  })

  it('still reads as disabled: its edge is drawn unlike the enabled switch\'s', () => {
    const off = painted(mergeSwitch(true), ['border', 'border-style'], WIDE) ?? ''
    const on = painted(mergeSwitch(false), ['border', 'border-style'], WIDE) ?? ''
    // MUTATION: draw the disabled switch with the enabled one's solid edge -> a live switch and an inert one look alike.
    expect(off).toMatch(/\bdashed\b/)
    expect(on).not.toMatch(/\bdashed\b/)
    expect(painted(mergeSwitch(true), 'cursor', WIDE)).toBe('not-allowed')
  })
})

// ---------------------------------------------------------------------------
// N26
// ---------------------------------------------------------------------------

describe('N26: "lands as" speaks of waiting for a room of 0 and for a room not read', () => {
  it('room 0 (claude-code): it waits for room', async () => {
    const container = await form()
    fireEvent.click(radio(container, 'claude-code'))
    // MUTATION: drop the room-0 clause -> a full pool says nothing about not starting yet.
    expect(landsAs(container)).toBe('lands asready or parked · waits for room · costs nothing')
  })

  it('room unknown (claude-code-browser): it may wait, because the room was not read', async () => {
    const container = await form()
    fireEvent.click(radio(container, 'claude-code-browser'))
    // MUTATION: test `headroomFor(profile).agents === 0` alone again -> an unread room says nothing (the run-4 line).
    expect(landsAs(container)).toBe('lands asready or parked · may wait for room: room not read · costs nothing')
  })

  it('room to spare (mock): the plain line', async () => {
    const container = await form()
    fireEvent.click(radio(container, 'mock'))
    // MUTATION: say a wait unconditionally -> a runner with room is told it waits.
    expect(landsAs(container)).toBe('lands asready or parked · costs nothing')
  })
})

// ---------------------------------------------------------------------------
// c09
// ---------------------------------------------------------------------------

const C09 = 'It is accepted and waits, holding nothing, until every pool it needs has room.'
const ADMITS = 'Nothing runs until the scheduler admits it into every pool it needs.'

/** The notes drawn after the send button, in order. */
function notesUnderButton(container: HTMLElement): HTMLElement[] {
  const go = screen.getByRole('button', { name: 'Start one agent' })
  const send = container.querySelector<HTMLElement>('.sbf-send')!
  return [...send.querySelectorAll<HTMLElement>('p.sb-note')].filter(
    (p) => (go.compareDocumentPosition(p) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0,
  )
}

describe('c09: with a measured room of 0 the note under the button says it is accepted and waits', () => {
  it('room 0: the decided sentence, in the warning ink, on desktop and phone', async () => {
    const container = await form()
    fireEvent.click(radio(container, 'claude-code'))
    // The button is unchanged.
    expect(screen.getByRole('button', { name: 'Start one agent' })).not.toBeNull()
    const notes = notesUnderButton(container)
    const note = notes.find((p) => visible(p) === C09)
    // MUTATION: keep the admits note at room 0 -> nothing under the button says the task will wait.
    expect(note, `the notes under the button: ${JSON.stringify(notes.map(visible))}`).toBeDefined()
    expect(notes.map(visible)).not.toContain(ADMITS)
    for (const env of [WIDE, PHONE]) {
      const ink = painted(note!, 'color', env)
      expect(ink).not.toBeNull()
      for (const theme of THEMES) {
        // MUTATION: draw the note in `.sb-note`'s `--text-dim` -> the sentence reads as a footnote.
        expect(resolveColour(ink!, theme), `${theme} at ${env.width}: the note's ink`).toEqual(resolveColour('var(--warn-ink)', theme))
      }
    }
  })

  it('room unknown and room to spare keep the admits note, and the sentence follows each pick', async () => {
    const container = await form()
    fireEvent.click(radio(container, 'claude-code'))
    expect(notesUnderButton(container).map(visible)).toContain(C09)
    for (const name of ['claude-code-browser', 'mock']) {
      fireEvent.click(radio(container, name))
      const said = notesUnderButton(container).map(visible)
      // MUTATION: test `agents === 0 || agents === null` for the note -> an unread room is told it has room for 0.
      expect(said, `${name}: the room-0 sentence`).not.toContain(C09)
      expect(said, `${name}: the admits note`).toContain(ADMITS)
    }
  })
})

// ---------------------------------------------------------------------------
// N27
// ---------------------------------------------------------------------------

type Rect = { top: number; bottom: number }

/** Give elements boxes jsdom cannot lay out: `boxOf` answers per element, null for the default. */
function boxes(boxOf: (el: Element) => Rect | null) {
  const real = Element.prototype.getBoundingClientRect
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
    const r = boxOf(this)
    if (r === null) return real.call(this)
    return { top: r.top, bottom: r.bottom, left: 0, right: 390, width: 390, height: r.bottom - r.top, x: 0, y: r.top, toJSON: () => ({}) } as DOMRect
  })
}

/** The page's scroller, as the console draws it, with a scrollTop jsdom keeps. */
function scroller(start: number): { el: HTMLElement; at: () => number; writes: number[] } {
  const el = document.createElement('div')
  el.className = 'ctl-scroll'
  document.body.appendChild(el)
  hosts.push(el)
  let top = start
  const writes: number[] = []
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => top,
    set: (v: number) => {
      top = v
      writes.push(v)
    },
  })
  return { el, at: () => top, writes }
}

describe('N27: at 390 a runner pick stays on the pick and the reason is at its field', () => {
  it('calls no scroll that sends the page to its top', async () => {
    const into = vi.fn()
    const to = vi.fn()
    Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: into })
    Object.defineProperty(window, 'scrollTo', { configurable: true, value: to })
    const port = scroller(900)
    const container = await form(port.el)
    into.mockClear()
    to.mockClear()
    fireEvent.click(radio(container, 'mock'))
    fireEvent.click(radio(container, 'claude-code'))
    const toTop = [...into.mock.calls, ...to.mock.calls].filter(([o]) =>
      o === true || o === undefined || (typeof o === 'object' && o !== null && ((o as ScrollIntoViewOptions).block === 'start' || (o as ScrollToOptions).top === 0)))
    // MUTATION: scroll the form or the scroller to its top on a pick -> the run-4 jump.
    expect(toTop, 'a scroll call that sends the page to its top').toEqual([])
    // MUTATION: set the scroller's scrollTop to 0 on a pick -> the page returns to its top.
    expect(port.writes.filter((v) => v === 0)).toEqual([])
    delete (Element.prototype as Partial<Element>).scrollIntoView
  })

  it('puts the picked card back where it was when the page moved under it', async () => {
    const port = scroller(900)
    const container = await form(port.el)
    const card = radio(container, 'mock').closest('.sbf-runner')!
    // The card was at 300 when it was pressed; the render moved it to -200.
    let reads = 0
    boxes((el) => {
      if (el === port.el) return { top: 0, bottom: 844 }
      if (el === card) return reads++ === 0 ? { top: 300, bottom: 360 } : { top: -200, bottom: -140 }
      return null
    })
    fireEvent.click(radio(container, 'mock'))
    // MUTATION: drop the pick's layout effect -> scrollTop stays 900 and the card is above the fold.
    expect(port.at(), 'the scroller moved the card back by what the render moved it').toBe(400)
  })

  it('brings the runner\'s detail into view under the pick', async () => {
    const port = scroller(900)
    const container = await form(port.el)
    const card = radio(container, 'mock').closest('.sbf-runner')!
    boxes((el) => {
      if (el === port.el) return { top: 0, bottom: 844 }
      if (el === card) return { top: 600, bottom: 660 }
      if (el.classList.contains('sbf-room')) return { top: 700, bottom: 1000 }
      return null
    })
    fireEvent.click(radio(container, 'mock'))
    // 156px of the detail is below the fold, and the card has 600px above it to give.
    // MUTATION: drop the detail's nudge -> the detail stays below the fold, as run 4 saw.
    expect(port.at()).toBe(900 + 156)
  })

  it('draws the reason in the step that holds its field below 1080, and only there', async () => {
    const container = await form()
    const steps = () => [...container.querySelectorAll<HTMLElement>('form.sbf .sb-step')]
    const at = (i: number) => [...steps()[i]!.querySelectorAll<HTMLElement>('.sbf-why-at')]
    // No runner: step 1 says so.
    // MUTATION: draw the reason only in the send panel -> at 390 it is three screens below step 1.
    expect(at(0).map(visible)).toEqual(['Not ready: choose a runner.'])
    fireEvent.click(radio(container, 'claude-code'))
    // claude-code requires a prompt: step 2, where the prompt is, says it.
    expect(at(0)).toEqual([])
    expect(at(1).map(visible)).toEqual(['Not ready: fill in prompt.'])
    const line = at(1)[0]!
    // MUTATION: hide `.sbf-why-at` at every width -> the phone reader never sees it.
    expect(painted(line, 'display', PHONE) ?? 'block').not.toBe('none')
    expect(painted(line, 'display', { width: 1000 }) ?? 'block').not.toBe('none')
    // From 1080 the send panel is sticky beside the steps, so the line would be a second copy in view.
    // MUTATION: drop the 1080 rule -> the desktop draws the reason twice on one screen.
    expect(painted(line, 'display', WIDE)).toBe('none')
    // The send panel keeps the whole line.
    expect(visible(container.querySelector('.sbf-why'))).toBe('Not ready: fill in prompt.')
  })
})
