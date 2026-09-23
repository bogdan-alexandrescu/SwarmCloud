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

/**
 * `work/running`, NOT `agents/running`, AND THE CHANGE IS THE POINT OF THE
 * RENAME RATHER THAN A CONCESSION TO IT.
 *
 * This list is of CANONICAL spellings -- hashes the address bar is left showing
 * unchanged. `agents` stopped being one when the section became `work`: it is a
 * key of `SECTION_ALIASES` now, so `#agents/running` resolves (a link saved in a
 * runbook still lands) and is then rewritten to `#work/running`, which is
 * exactly what an alias is for. Asserting that it round-trips UNCHANGED asserts
 * that the alias is a second permanent name, which is the defect
 * `INTERNAL_LINKS_MAY_NOT_USE_ALIASES` exists to prevent.
 *
 * The alias's own behaviour is not lost by this edit -- `adding Help did not
 * capture anything that was not Help` below still drives `#agents/running`
 * through `fromHash` and checks where it lands.
 *
 * This assertion has been red since the rename landed, on every branch cut from
 * it. Fixed here by the overflow lane because it was red in front of that
 * lane's own gate; it is the nav rename's debt, not that lane's work.
 */
test('a route round-trips through its canonical spelling', () => {
  for (const hash of ['help', `help/${TOPIC_IDS[0]}`, 'reference', 'work/running']) {
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
    ['#agents/running', 'agents'],
    ['#helpers', 'overview'], // a head that merely STARTS with "help"
    ['#capacity/holders', 'pools'], // the alias, tail intact
  ] as const) {
    at(hash)
    assert.equal(fromHash().sectionId, section, `${hash} no longer resolves as it did`)
  }
  at('#capacity/holders')
  assert.equal(fromHash().tab, 'holders', 'the alias lost its tail')
})

test('the agent drawer still resolves, including a task id spelling "help"', () => {
  at('#agents/task/help')
  const r = fromHash()
  assert.equal(r.sectionId, 'agents')
  assert.equal(r.taskId, 'help')
})
