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
 * A CANONICAL ROUTE IS UNCHANGED BY `canonical`; AN ALIAS IS NOT, AND MUST NOT BE.
 *
 * This list held `agents/running` and asserted it round-tripped untouched. That
 * was true when it was written and stopped being true when the nav rename made
 * `agents` an alias of `work` -- so the assertion has been failing on this
 * branch since the rename, pinning a SPELLING that the rename deliberately
 * retired rather than the RULE the spelling was an example of.
 *
 * The rule has two halves and they are opposite, which is why one list could
 * not hold both. A canonical route must survive `canonical` untouched, or the
 * address bar drifts from the address people paste. An ALIAS must NOT: the
 * whole point of SECTION_ALIASES is that a link someone saved before the rename
 * still lands, and that the address bar then teaches them the new spelling. An
 * alias that round-tripped would be a second permanent name for one screen,
 * which is the two-spellings defect the rename existed to remove.
 */
test('a route round-trips through its canonical spelling', () => {
  for (const hash of ['help', `help/${TOPIC_IDS[0]}`, 'reference']) {
    at(`#${hash}`)
    assert.equal(canonical(fromHash()), hash, `#${hash} is rewritten to something else`)
  }
})

test('an alias resolves, and is rewritten to the name that replaced it', () => {
  // The tail is carried through untouched: a rename moves the section, never
  // the pane, so a saved deep link lands on the pane it named.
  at('#agents/running')
  const rewritten = canonical(fromHash())
  assert.notEqual(rewritten, 'agents/running', 'the alias is still a canonical spelling')
  assert.ok(rewritten.endsWith('/running'), `the alias lost its tail: ${rewritten}`)
  // ...and the rewritten form is itself canonical, so following it settles
  // rather than bouncing between two names.
  at(`#${rewritten}`)
  assert.equal(canonical(fromHash()), rewritten, `#${rewritten} is rewritten again`)
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
