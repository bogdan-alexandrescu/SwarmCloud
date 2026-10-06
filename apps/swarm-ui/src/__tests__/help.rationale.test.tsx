/**
 * HELP SAYS WHAT A STATE MEANS; WHY THE UI DRAWS IT SO LIVES IN docs/ (#131).
 *
 * The issue: "the long forms mostly give rationale ('A screen that drew a file
 * tree here would be inventing it')". A reader on the Help page wants to know
 * what they are looking at and what to do. Why the console was built to draw
 * it that way -- the counterfactual screen that would have lied, the bug the
 * app exists to prevent -- is design rationale, and it is kept, word for
 * word, in docs/web-ui/help-rationale.md, where the next person tempted to
 * revert a decision will look.
 *
 * The pattern is the shape that rationale took: a hypothetical screen, panel,
 * chip, console or button that would have drawn something wrong, and the
 * app's own history. It does NOT ban saying what the console does now
 * ("this app never claims to"), nor a platform rule's rationale ("a console
 * that answered differently would be an oracle" is about the API's refusal).
 * The controls pin that boundary both ways.
 *
 * MUTATION: put "A screen that drew a file tree here would be inventing it."
 * back into `checkpoints`, or delete a topic's section from the doc. The
 * verbs include `showed` and `asked`, and the hand-written-description form:
 * the review of #131 found three paragraphs the narrower pattern let through.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

import { HELP, TOPIC_IDS, type TopicId } from '../help'

const DOC = join(__dirname, '..', '..', '..', '..', 'docs', 'web-ui', 'help-rationale.md')

const UI_RATIONALE =
  /\bthis UI’s\b|\b(this|the) (whole )?app (exists|is about)\b|\bwould be inventing\b|\b(a|any) (screen|panel|chip|console|button) that (drew|draws|read|reads|reported|kept|fell|says|showed|shows|asked|asks)\b|\bhand-written description would be\b|\bwhy the screens\b|\bthe intended cost\b/i

/** The topics whose UI rationale moved to the doc, and a phrase of what moved. */
const MOVED: Readonly<Record<string, string>> = {
  'absent-vs-zero': 'defining bug',
  'api-reads': 'reporting itself broken',
  'read-failed': 'This app exists because of one bug',
  checkpoints: 'would be inventing it',
  'all-clear-basis': 'least trustworthy',
  'paused-vs-full': 'treatment of its own',
  'not-a-machine-inventory': 'would be inventing them',
  'catalogue-from-route': 'correct until the day it mattered',
  'runtime-needs-no-provider': 'we could not find out',
  'clipboard-secure-context': 'the small version of the bug',
  'withheld-total': 'deliberately more annoying',
  'provider-quota-states': 'fell through to',
  'what-sets-it-apart-is-arithmetic': 'could quietly stop being true',
  'sign-in-not-paste': 'handle key material by hand',
  'ceiling-change-evicts-nothing': 'as breakage',
  'credential-split': 'for no operational benefit',
}

describe('Help’s long form carries no UI rationale (#131)', () => {
  it('the pattern catches the rationale and spares what is true on the Help page', () => {
    for (const bad of [
      'drawing them alike was this UI’s defining bug',
      'A screen that drew a file tree here would be inventing it.',
      'A panel that draws them alike is at its least trustworthy',
      'a console that reported that as a fault would be reporting itself broken',
      'A console that kept its own copy would be correct until the day it mattered.',
      'That is the intended cost',
      'the small version of the bug this whole app is about',
      'the failure this whole app exists to prevent',
      'A chip that fell through to "unknown"',
      'That is why the screens draw a paused pool',
      'A screen that showed that as a fault would be reporting an operator’s own action back to them as breakage.',
      'A hand-written description would be the one thing on the Runtimes screen that could quietly stop being true.',
      'A screen that asked for a pasted credential was asking a person to handle key material by hand',
      'A console that showed a length would be publishing a fact about a secret',
    ]) {
      expect(UI_RATIONALE.test(bad), `the pattern misses "${bad}"`).toBe(true)
    }
    for (const fine of [
      'the infrastructure cost of a run is recorded nowhere this app can read',
      'so this app never claims to',
      'A console that answered differently would be an oracle for other tenants’ account labels.',
      'a console that wrote both would be able to invent capacity',
    ]) {
      expect(UI_RATIONALE.test(fine), `the pattern catches "${fine}", which is not UI rationale`).toBe(false)
    }
  })

  it('no long paragraph of any topic gives it', () => {
    let read = 0
    const found: string[] = []
    for (const id of TOPIC_IDS) {
      HELP[id].long.forEach((para, i) => {
        read++
        const m = UI_RATIONALE.exec(para)
        if (m) found.push(`${id} long[${i}]: "${m[0]}"`)
      })
    }
    // A walk over nothing passes hardest (tests/help.test.ts measured 243).
    expect(read).toBeGreaterThanOrEqual(243)
    expect(found).toEqual([])
  })

  it('every rationale that left a topic is in docs/web-ui/help-rationale.md, under that topic', () => {
    const doc = readFileSync(DOC, 'utf8')
    const sections = doc.split(/^## /m).slice(1)
    for (const [id, phrase] of Object.entries(MOVED)) {
      expect(HELP[id as TopicId], `${id} is not a topic`).toBeDefined()
      const section = sections.find((s) => s.startsWith(`\`${id}\``))
      expect(section, `the doc has no section for ${id}`).toBeDefined()
      // Markdown wraps a quote across `> ` lines; read it as one line.
      const flat = section!.replace(/\s*\n>?\s*/g, ' ')
      expect(flat, `${id}'s section does not keep what moved`).toContain(phrase)
      // And it is gone from the Help page.
      expect(HELP[id as TopicId].long.join(' ')).not.toContain(phrase)
    }
    // api-reads gave two; the second is the p95's counterfactual.
    const reads = sections.find((s) => s.startsWith('`api-reads`'))!.replace(/\s*\n>?\s*/g, ' ')
    expect(reads).toContain('the same class of claim')
    expect(HELP['api-reads'].long.join(' ')).not.toContain('the same class of claim')
  })
})
