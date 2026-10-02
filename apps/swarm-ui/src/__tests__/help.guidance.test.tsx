/**
 * HELP READS AS OPERATOR GUIDANCE, WITH SHORT FOOTERS (#131).
 *
 *   * EVERY TOPIC ENDS IN AN ACTION. `act` is what to do or where to look,
 *     and it is the last thing the topic draws before its anchor. Where a
 *     screen answers it, the action links to that screen by a route the
 *     router knows.
 *   * EVERY TOPIC HAS A SUBJECT of two to six words -- what `HelpLinks`
 *     promises -- and the footer indexes print the subject, not the claim.
 *     The claim (`title`) is unchanged: the `?` cards share it word for word.
 *   * The four topics the issue named as missing exist and say their piece.
 *
 * MUTATION: delete one topic's `act` or `subject` and tsc fails; give one an
 * eight-word subject, or an `at` the router does not know, and this does.
 * Print `title` in HelpLinks again and the footer case goes red.
 */
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import { HELP, HELP_GROUPS, HELP_PLACES, TOPIC_IDS, type HelpPlace } from '../help'
import { HelpLinks } from '../HelpCard'
import { HelpScreen } from '../HelpSection'
import { addressToPath } from '../paths'

afterEach(cleanup)

const words = (s: string) => s.trim().split(/\s+/).length

/** Every group page Help draws (H1: one page per group), mounted together. */
function renderEveryGroup(): void {
  render(
    <>
      {HELP_GROUPS.map((g) => (
        <HelpScreen key={g.id} topic={g.id} />
      ))}
    </>,
  )
}

describe('every Help topic ends in what to do (#131)', () => {
  it('carries an action, and the action is the card’s last row', () => {
    renderEveryGroup()
    for (const id of TOPIC_IDS) {
      const t = HELP[id]
      expect(t.act.say.trim().length, `${id} has no action`).toBeGreaterThan(0)
      const block = document.getElementById(t.anchor)
      expect(block, `${id} is on no group page`).not.toBeNull()
      // H1: the rows are You see / It means / What to do, and the anchor is
      // in the card's head beside its name.
      const rows = [...block!.querySelectorAll(':scope > dl.help-rows > div')]
      const act = block!.querySelector('.help-topic-act')
      expect(act, `${id} draws no action`).not.toBeNull()
      expect(rows.indexOf(act!), `${id}'s action is not its last row`).toBe(rows.length - 1)
      expect(block!.querySelector(':scope > .help-topic-head > .help-topic-anchor'), `${id} has no anchor in its head`).not.toBeNull()
      expect(act!.textContent).toContain(t.act.say)
    }
  })

  it('links the screen that answers it, by a route the router knows', () => {
    renderEveryGroup()
    const places = Object.keys(HELP_PLACES) as HelpPlace[]
    for (const place of places) {
      // `/overview` is the router's fallback for an address it cannot read.
      if (place !== 'overview/now') expect(addressToPath(place), `${place} is not a route`).not.toBe('/overview')
    }
    let linked = 0
    for (const id of TOPIC_IDS) {
      const at = HELP[id].act.at
      if (at === undefined) continue
      linked++
      const a = document.getElementById(HELP[id].anchor)!.querySelector<HTMLAnchorElement>('.help-topic-act a')
      expect(a, `${id} names a screen and does not link it`).not.toBeNull()
      expect(a!.getAttribute('href')).toBe(`#${at}`)
      expect(a!.textContent).toBe(`Open ${HELP_PLACES[at]}`)
    }
    expect(linked, 'no topic links the screen that answers it').toBeGreaterThan(TOPIC_IDS.length / 2)
  })

  it('names no place the Help page is not (the AH-13 rule, for the action too)', () => {
    const POINTS = /\bthis (screen|panel|page|form|field|table|card)\b|\bshown above\b|\bbelow it\b|\bused to (say|show)\b/i
    for (const id of TOPIC_IDS) expect(HELP[id].act.say, id).not.toMatch(POINTS)
  })
})

describe('every Help topic has a two-to-six-word subject (#131)', () => {
  it('is 2-6 words for every topic, and no two topics share one', () => {
    for (const id of TOPIC_IDS) {
      const n = words(HELP[id].subject)
      expect(n >= 2 && n <= 6, `${id}: "${HELP[id].subject}" is ${n} words`).toBe(true)
    }
    const all = TOPIC_IDS.map((id) => HELP[id].subject.toLowerCase())
    expect(new Set(all).size).toBe(all.length)
  })

  it('is what a footer index prints, with the claim kept on hover', () => {
    const { container } = render(
      <HelpLinks topics={['lease-and-pool-are-two-records', 'paused-vs-full', 'unreadable-documents']} />,
    )
    const links = [...container.querySelectorAll('a')]
    expect(links.map((a) => a.textContent)).toEqual([
      HELP['lease-and-pool-are-two-records'].subject,
      HELP['paused-vs-full'].subject,
      HELP['unreadable-documents'].subject,
    ])
    for (const a of links) {
      expect(words(a.textContent ?? '')).toBeLessThanOrEqual(6)
      expect(a.getAttribute('title')).toBe(Object.values(HELP).find((t) => `#${t.anchor}` === a.getAttribute('href'))!.title)
    }
  })
})

describe('the topics #131 found missing are there (#131)', () => {
  it('has paused vs full, what a pool is, the Tenants fields and Platform counts', () => {
    expect(HELP['paused-vs-full'].long.join(' ')).toMatch(/paused/i)
    expect(HELP['paused-vs-full'].long.join(' ')).toMatch(/full/i)
    expect(HELP['what-a-pool-is'].long.join(' ')).toMatch(/units/i)
    expect(HELP['tenant-fields'].long.join(' ')).toMatch(/Enforced/)
    expect(HELP['platform-counts'].long.join(' ')).toMatch(/count\(\)/)
    // Each ends in the screen that answers it.
    expect(HELP['paused-vs-full'].act.at).toBe('capacity/pools')
    expect(HELP['tenant-fields'].act.at).toBe('admin/tenants')
    expect(HELP['platform-counts'].act.at).toBe('admin/counts')
  })
})
