// QA1-FIX-35 (visual QA 2026-10-28 pass 1, part of #2596 and #2391): two
// findings whose words live in help.ts and lanes.ts.
//
//   * config-32: every Config tab's head `?` card was titled "Config answers"
//     over the same section question, so six pages opened what read as one
//     card. A page with a topic of its own is now titled by its tab.
//   * runs-submit-34: the Lanes banner read "Past 7 days the bars are
//     slivers" on a 30d span. It now names the span drawn.

import { describe, expect, it } from 'vitest'

import { HELP, PAGE_TOPIC, pageQuestionTitle, pageTopic } from '../help'
import { DEFAULT_LANES_VIEW, SUGGEST_OUTCOMES_DAYS, parseLanesView, sliversNote, spanDays } from '../lanes'

const CONFIG_TABS = [
  ['github', 'GitHub access'],
  ['repositories', 'Repositories'],
  ['secrets', 'Secrets'],
  ['mcp-servers', 'MCP servers'],
  ['people', 'People and roles'],
  ['workspace', 'Workspace Settings'],
] as const

describe('config-32: a Config page’s `?` card is titled by its tab', () => {
  it('gives the six Config tabs six titles, each naming its tab', () => {
    const titles = CONFIG_TABS.map(([tab, label]) => pageQuestionTitle('Config', 'config', tab, label))
    // MUTATION: return `${sectionLabel} answers` always and all six read "Config answers".
    expect(titles).toEqual(CONFIG_TABS.map(([, label]) => `${label} answers`))
    expect(new Set(titles).size).toBe(CONFIG_TABS.length)
  })

  it('keeps the section’s title on a page with no topic of its own', () => {
    // MUTATION: drop the `pageTopic(...) !== null` test and Capacity › Pools reads "Pools answers"
    // over a card that is the section's question and nothing else.
    expect(pageQuestionTitle('Capacity', 'capacity', 'pools', 'Pools')).toBe('Capacity answers')
    expect(pageQuestionTitle('Config', 'config', 'secrets', undefined)).toBe('Config answers')
  })

  it('names a real topic for every page it keys, and none for an unkeyed one', () => {
    for (const [key, id] of Object.entries(PAGE_TOPIC)) expect(HELP[id], key).toBeDefined()
    expect(pageTopic('config', 'secrets')).toBe('secret-scopes')
    expect(pageTopic('capacity', 'pools')).toBeNull()
    expect(pageTopic('config', null)).toBeNull()
  })
})

describe('runs-submit-34: the Lanes banner names the span drawn', () => {
  it('says "Over 30 days" on a 30d span, never "Past 7 days"', () => {
    const now = Date.parse('2026-10-28T10:00:00Z')
    const days = spanDays(parseLanesView('span=30d'), now)
    expect(days).toBeGreaterThan(SUGGEST_OUTCOMES_DAYS)
    // MUTATION: restore `Past ${SUGGEST_OUTCOMES_DAYS} days` and this reads "Past 7 days".
    expect(sliversNote(days)).toBe('Over 30 days the bars are slivers: counts over the span read better as a ledger.')
    expect(sliversNote(days)).not.toMatch(/Past 7/)
  })

  it('names each wide span, and a zoomed window to a tenth of a day', () => {
    const now = Date.parse('2026-10-28T10:00:00Z')
    expect(sliversNote(spanDays(parseLanesView('span=14d'), now))).toMatch(/^Over 14 days /)
    expect(sliversNote(spanDays(parseLanesView('span=90d'), now))).toMatch(/^Over 90 days /)
    expect(sliversNote(spanDays({ ...DEFAULT_LANES_VIEW, span: null, since: '2026-10-01T00:00:00Z', until: '2026-10-09T06:00:00Z' }, now))).toMatch(/^Over 8.3 days /)
  })
})
