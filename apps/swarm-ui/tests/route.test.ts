/**
 * `#help/<topic>` HAS TO LAND.
 *
 * Every `?` card in the product ends in a link to one topic, and every link is
 * GENERATED -- built from the topic id, never typed. That makes a broken link
 * invisible in review: nothing looks wrong in the card, and the only symptom
 * is that the reader who follows it arrives on Home. So the route gets its own
 * test rather than being trusted because it is four lines.
 *
 * The hash IS the router here (there is no react-router), so `fromHash` reads
 * `window.location.hash`. There is no DOM under node, so the tests install the
 * two properties it actually touches. That is the whole of its contact with a
 * browser, which is why this is a stub rather than a shortcoming.
 */
import assert from 'node:assert/strict'
import test from 'node:test'

function at(hash: string): void {
  ;(globalThis as { window?: unknown }).window = { location: { hash } }
}

// The stub has to exist before App.tsx's module body runs -- it reads nothing
// at import time today, but an import that did would fail here rather than in
// a browser, which is the right place for it to fail.
at('')
const { fromHash, canonical } = await import('../src/App')
const { HELP_ROUTE, TOPIC_IDS, helpAnchor } = await import('../src/help')

test('every topic anchor routes to the Help section, carrying its topic', () => {
  for (const id of TOPIC_IDS) {
    at(`#${helpAnchor(id)}`)
    const r = fromHash()
    assert.equal(r.sectionId, HELP_ROUTE, `#${helpAnchor(id)} does not reach Help`)
    assert.equal(r.tab, id, `#${helpAnchor(id)} lost its topic`)
    assert.equal(r.taskId, null)
  }
})

test('the bare #help route reaches the Help section', () => {
  at('#help')
  const r = fromHash()
  assert.equal(r.sectionId, HELP_ROUTE)
  assert.equal(r.tab, '')
})

test('a route round-trips through its canonical spelling', () => {
  for (const hash of ['help', `help/${TOPIC_IDS[0]}`, 'reference', 'agents/running']) {
    at(`#${hash}`)
    assert.equal(canonical(fromHash()), hash, `#${hash} is rewritten to something else`)
  }
})

test('an unknown topic is carried to the screen, not rewritten away', () => {
  // The screen names what was asked for. Quietly normalising it to the top of
  // the page would turn a stale link into a page that looks right and answers
  // nothing -- and would hide the rename from whoever has to fix the link.
  at('#help/a-topic-that-was-renamed')
  const r = fromHash()
  assert.equal(r.sectionId, HELP_ROUTE)
  assert.equal(r.tab, 'a-topic-that-was-renamed')
})

test('adding Help did not capture anything that was not Help', () => {
  for (const [hash, section] of [
    ['#reference', 'reference'],
    ['#work/running', 'work'],
    ['#helpers', 'overview'], // a head that merely STARTS with "help"
    ['#capacity/holders', 'capacity'],
    // BOTH RETIRED SPELLINGS, with their tails intact. These are the hashes in
    // runbooks and in the links people paste to each other at 3am; `capacity`
    // is a third, from before an earlier pass renamed it to `pools` and this
    // one renamed it back. An alias that drops the tail lands the reader on
    // the section's first pane, which looks like a working link to the wrong
    // screen -- worse than a dead one.
    ['#agents/running', 'work'],
    ['#pools/holders', 'capacity'],
  ] as const) {
    at(hash)
    assert.equal(fromHash().sectionId, section, `${hash} no longer resolves as it did`)
  }
  for (const hash of ['#capacity/holders', '#pools/holders'] as const) {
    at(hash)
    assert.equal(fromHash().tab, 'holders', `${hash} lost its tail`)
  }
})

test('the agent drawer still resolves, including a task id spelling "help"', () => {
  at('#work/task/help')
  const r = fromHash()
  assert.equal(r.sectionId, 'work')
  assert.equal(r.taskId, 'help')

  // AND THROUGH THE ALIAS. `#agents/task/<id>` is the address in every saved
  // deep link and in the audit evidence. `fromHash` resolves the alias BEFORE
  // the drawer branch runs for exactly this reason: when the branch tested the
  // raw head it was the one place still answering to the old name, and this
  // hash would have resolved to the section with the task id dropped --
  // opening the list instead of the agent, silently.
  at('#agents/task/help')
  const viaAlias = fromHash()
  assert.equal(viaAlias.sectionId, 'work')
  assert.equal(viaAlias.taskId, 'help')
})
