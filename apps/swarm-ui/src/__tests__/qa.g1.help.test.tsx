/**
 * HELP, FROM THE QA PASS ON swarm.saga.xyz (2026-10-07).
 *
 *   G1-16  a topic's "You see" row repeated its card's name word for word in
 *          five topics -- the row is the on-screen claim, not the name again;
 *   G1-17  search matched inside words ("lease" lit "re<lease>d") and cut its
 *          snippets mid-word ("…ounts read at one moment");
 *   G1-18  an unknown topic carried the `real zero` mark, which means a
 *          measured zero, where nothing was measured;
 *   G1-21  the keyboard shortcuts were in no topic, and no key opened one.
 *
 * Each block names the mutation that turns it red.
 */
import AGENT_LOGS from '../AgentLogs.tsx?raw'
import AGENT_SPLIT from '../AgentSplit.tsx?raw'
import DIFF_VIEW from '../diff/DiffView.tsx?raw'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { HELP, HELP_GROUPS, TOPIC_IDS, helpAnchor, type TopicId } from '../help'
import { HelpScreen } from '../HelpSection'
import { SCOPE_WORDS, SHORTCUTS, shortcutsIn } from '../shortcuts'
import { SkyShell } from '../Spine'
import { SubmitChooser } from '../SubmitChooser'

afterEach(() => {
  cleanup()
  window.location.hash = ''
})

const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()

describe('G1-16: "You see" is the claim, never the name again', () => {
  /** MUTATION: set any of the five titles back to its subject. */
  it('no topic’s title is its subject', () => {
    const same = TOPIC_IDS.filter((id) => HELP[id].title.trim().toLowerCase() === HELP[id].subject.trim().toLowerCase())
    expect(TOPIC_IDS.length).toBeGreaterThan(90)
    expect(same).toEqual([])
  })

  it('draws a different "You see" line from the heading on every card of every group', () => {
    let cards = 0
    for (const g of HELP_GROUPS) {
      render(<HelpScreen topic={g.id} />)
      for (const card of document.querySelectorAll('.help-topic')) {
        cards++
        const heading = text(card.querySelector('.help-topic-head h3'))
        const see = text(card.querySelector('dl.help-rows > div:first-child > dd'))
        expect(see, `${card.id} repeats its heading`).not.toBe(heading)
      }
      cleanup()
    }
    expect(cards).toBe(TOPIC_IDS.length)
  })
})

/** Every text a topic is searched in, as `HelpSection` reads them. */
const textsOf = (id: TopicId): string[] => [HELP[id].subject, HELP[id].title, HELP[id].short, ...HELP[id].long]

function search(q: string): HTMLAnchorElement[] {
  render(<HelpScreen topic="" />)
  fireEvent.change(screen.getByRole('searchbox'), { target: { value: q } })
  return [...document.querySelectorAll<HTMLAnchorElement>('.help-hits a.help-hit')]
}

const WORD = /[\p{L}\p{N}]/u

describe('G1-17: search matches whole words first, and snippets end on word edges', () => {
  /** MUTATION: go back to a plain `indexOf` over the lowercased text. */
  it('“lease” does not light the inside of “released”', () => {
    const hits = search('lease')
    expect(hits.length).toBeGreaterThan(0)
    for (const a of hits) {
      const mark = a.querySelector('mark')!
      const before = mark.previousSibling?.textContent ?? ''
      expect(before === '' || before.endsWith('…') || !WORD.test(before.slice(-1)), `${a.getAttribute('href')} marks inside a word: “${text(a.querySelector('span'))}”`).toBe(true)
    }
    // ...and the topics whose only "lease" is inside another word are not hits.
    const onlyInside = TOPIC_IDS.filter(
      (id) => textsOf(id).some((t) => /lease/i.test(t)) && !textsOf(id).some((t) => /(?<![\p{L}\p{N}])lease/iu.test(t)),
    )
    for (const id of onlyInside) expect(hits.map((a) => a.getAttribute('href'))).not.toContain(`#${HELP[id].anchor}`)
  })

  /** MUTATION: drop the substring fallback, and a fragment finds nothing. */
  it('falls back to a match inside a word when no word starts with the query', () => {
    const hits = search('ease')
    expect(TOPIC_IDS.some((id) => textsOf(id).some((t) => /(?<![\p{L}\p{N}])ease/iu.test(t)))).toBe(false)
    expect(hits.length).toBeGreaterThan(0)
    for (const a of hits) expect(text(a.querySelector('mark')).toLowerCase()).toBe('ease')
  })

  /** MUTATION: slice the snippet at a fixed 48 and 72 characters again. */
  it('starts and ends every snippet on a word edge', () => {
    let checked = 0
    for (const q of ['read', 'pool', 'tenant', 'lease']) {
      for (const a of search(q)) {
        const id = TOPIC_IDS.find((t) => `#${HELP[t].anchor}` === a.getAttribute('href'))!
        const shown = (a.querySelector('span')!.textContent ?? '').replace(/^…/, '').replace(/…$/, '')
        const src = textsOf(id).find((t) => t.includes(shown))
        expect(src, `${id}: “${shown}” is not a slice of the topic`).toBeDefined()
        const at = src!.indexOf(shown)
        const end = at + shown.length
        expect(at === 0 || /\s/.test(src![at - 1]!), `${id} starts mid-word: “${shown.slice(0, 20)}”`).toBe(true)
        expect(end === src!.length || /\s/.test(src![end]!), `${id} ends mid-word: “${shown.slice(-20)}”`).toBe(true)
        checked++
      }
      cleanup()
    }
    expect(checked).toBeGreaterThan(20)
  })
})

describe('G1-18: an unknown topic is not-found, not a measured zero', () => {
  /** MUTATION: put `<Absent kind="zero">` back. */
  it('draws the not-found treatment NotFound.tsx draws, and no real-zero mark', () => {
    render(<HelpScreen topic="nope" />)
    expect(document.querySelector('.ctl-mark.is-zero'), 'the real-zero mark is on an unknown topic').toBeNull()
    const panel = document.querySelector('.c-emp.is-partial')
    expect(panel, 'not the not-found treatment').not.toBeNull()
    expect(text(panel!.querySelector('h3'))).toBe('No help topic is called nope')
    expect(panel!.querySelector('a[href="#help"]'), 'no way back to the top of Help').not.toBeNull()
  })
})

describe('G1-21: the keyboard has a topic, generated from the table the handlers read', () => {
  /** MUTATION: drop a row from the topic, or write its prose by hand. */
  it('lists every shortcut, with what it does and where', () => {
    const t = HELP['keyboard' as TopicId]
    expect(t, 'no keyboard topic').toBeDefined()
    const values = t.values?.() ?? []
    expect(values.length).toBe(SHORTCUTS.length)
    SHORTCUTS.forEach((s, i) => {
      expect(values[i]!.term).toBe(s.label)
      expect(values[i]!.note).toBe(`${s.does}, ${SCOPE_WORDS[s.scope]}`)
    })
    render(<HelpScreen topic="keyboard" />)
    const card = document.getElementById(helpAnchor('keyboard' as TopicId))!
    expect(card.querySelectorAll('.help-values > div').length).toBe(SHORTCUTS.length)
  })

  function shell(children: React.ReactNode = null) {
    const go = vi.fn()
    render(
      <SkyShell section="overview" tab="now" title="Overview" go={go} foot={null}>
        {children}
      </SkyShell>,
    )
    return go
  }

  /** MUTATION: drop the `?` branch from SkyShell's key handler. */
  it('? opens the keyboard topic from any page, Shift and all', () => {
    const go = shell()
    fireEvent.keyDown(document.body, { key: '?', shiftKey: true })
    expect(go).toHaveBeenCalledWith(helpAnchor('keyboard' as TopicId))
  })

  /** MUTATION: let `?` through from a field. */
  it('? is a character, not a command, in a field', () => {
    const go = shell(<input aria-label="field" />)
    fireEvent.keyDown(document.querySelector('input')!, { key: '?', shiftKey: true })
    expect(go).not.toHaveBeenCalled()
  })

  it('N still opens Submit, read from the table', () => {
    const go = shell()
    fireEvent.keyDown(document.body, { key: 'n' })
    expect(go).toHaveBeenCalledWith(SHORTCUTS.find((s) => s.scope === 'anywhere' && s.key === 'n')!.to)
  })

  /** MUTATION: give SubmitChooser its own key map again and change one key. */
  it('every Submit shortcut in the table is the key SubmitChooser answers', () => {
    const go = vi.fn()
    render(<SubmitChooser go={go} />)
    const keys = shortcutsIn('submit')
    expect(keys.length).toBe(3)
    for (const s of keys) {
      go.mockClear()
      fireEvent.keyDown(document.body, { key: s.key })
      expect(go).toHaveBeenCalledWith(s.to)
    }
  })

  /**
   * The keys that act inside one view are not navigation, and their handlers
   * hold state of their own; the table is held to them by their source.
   * MUTATION: rebind one of them in its handler without the table following.
   */
  it('names only keys the log, the diff and the agent split handle', () => {
    for (const s of shortcutsIn('log')) {
      expect(AGENT_LOGS.includes(`=== '${s.key}'`), `AgentLogs does not handle ${s.label}`).toBe(true)
    }
    for (const s of shortcutsIn('diff')) {
      expect(DIFF_VIEW.includes(`case '${s.key}':`), `DiffView does not handle ${s.label}`).toBe(true)
    }
    for (const s of shortcutsIn('agent')) {
      expect(AGENT_SPLIT.includes(`e.key !== '${s.key}'`), `AgentSplit does not handle ${s.label}`).toBe(true)
    }
  })
})
