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
  // CANONICAL SPELLINGS ONLY, and `work/running` replacing `agents/running`
  // here was not a typo fix -- it was a round trip being asked of a spelling
  // that is no longer canonical. `agents` became `work` and `pools` became
  // `capacity`; SECTION_ALIASES keeps both old spellings resolving and
  // `canonical` exists precisely to rewrite them. So the old entry asserted
  // that the alias machine does NOT work, and went red the moment the rename
  // landed: `#agents/running is rewritten to something else + 'work/running'`.
  // The alias's own property is the test below.
  for (const hash of ['help', `help/${TOPIC_IDS[0]}`, 'reference', 'work/running']) {
    at(`#${hash}`)
    assert.equal(canonical(fromHash()), hash, `#${hash} is rewritten to something else`)
  }
})

test('a retired spelling is rewritten to the current one, not merely accepted', () => {
  // THE HALF OF THE RENAME THAT IS EASY TO GET WRONG. Resolving an old hash is
  // not enough: if the address bar keeps saying `#agents/running`, then every
  // link copied out of it is a new link in the retired spelling, and the alias
  // outlives the rename by propagating itself. `App.tsx` normalises the hash
  // without adding history for exactly this reason, so the NEXT copy of a
  // pasted link is the current one.
  for (const [old, current] of [
    ['agents/running', 'work/running'],
    ['agents/workflows', 'work/workflows'],
    ['pools/holders', 'capacity/holders'],
    ['capacity/holders', 'capacity/holders'], // the middle spelling, now real again
    // THE THREE-SECTION COLLAPSE, 2026-09-24. `runtimes` and `history` stopped
    // being sections: Runtimes became a pane of Capacity, Timeline a pane of
    // Work, Platform counts a pane of Admin. Every one of these hashes is in a
    // runbook or an audit file somewhere, and every one of them has a TAIL
    // that has to survive -- an alias that drops the tail lands the reader on
    // the section's first pane, which looks like a working link to the wrong
    // screen.
    ['runtimes/catalogue', 'capacity/catalogue'],
    ['history/timeline', 'work/timeline'],
    // `activity` is the same pane one rename further back and now points two
    // renames forward: Activity -> History -> Work.
    ['activity/timeline', 'work/timeline'],
    // THE ONE A HEAD ALIAS CANNOT DO. History's two panes went to DIFFERENT
    // sections, so `history` aliases to `work` and this tail has to be
    // intercepted (App.tsx, MOVED_PANES) before that alias is applied.
    ['history/counts', 'admin/counts'],
    ['activity/counts', 'admin/counts'],
    // The bare top-level hash from the eleven-item nav, which LEGACY owns and
    // which followed its pane out of History into Admin.
    ['counts', 'admin/counts'],
    // The two-pane Settings split, which is the SAME shape as History's and is
    // asserted here because the collapse replaced its hand-rolled branch with
    // the shared MOVED_PANES map. Deleting either entry leaves `#settings/...`
    // resolving through LEGACY to Pool limits, so the accounts one would fail
    // loudly here and nowhere else.
    ['settings/accounts', 'capacity/accounts'],
    ['settings/limits', 'admin/limits'],
  ] as const) {
    at(`#${old}`)
    assert.equal(canonical(fromHash()), current, `#${old} is not rewritten to #${current}`)
  }

  // Including the drawer, whose id must survive the rewrite intact.
  at('#agents/task/task_abc123')
  assert.equal(canonical(fromHash()), 'work/task/task_abc123')

  // AND THE REWRITE IS IDEMPOTENT, which is the property that stops the two
  // names ping-ponging. Following a corrected address has to SETTLE: if the
  // canonical form of `work/running` were anything but itself, the normalise
  // effect in App.tsx would rewrite the hash on every resolve, and back/forward
  // would walk a chain of corrections instead of the routes someone visited.
  // (From the help-density lane, which found this file red independently.)
  for (const old of [
    'agents/running',
    'pools/holders',
    'agents/task/task_abc123',
    'history/timeline',
    'history/counts',
    'runtimes/catalogue',
  ]) {
    at(`#${old}`)
    const once = canonical(fromHash())
    at(`#${once}`)
    assert.equal(canonical(fromHash()), once, `#${once} is rewritten again`)
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
    // The two sections the 2026-09-24 collapse retired.
    ['#runtimes/catalogue', 'capacity'],
    ['#history/timeline', 'work'],
    ['#history/counts', 'admin'],
  ] as const) {
    at(hash)
    assert.equal(fromHash().sectionId, section, `${hash} no longer resolves as it did`)
  }
  for (const hash of ['#capacity/holders', '#pools/holders'] as const) {
    at(hash)
    assert.equal(fromHash().tab, 'holders', `${hash} lost its tail`)
  }
  // THE TAILS OF THE RETIRED SECTIONS, named one by one rather than left to
  // the round-trip test above. A saved `#history/timeline` has to open the
  // TIMELINE pane; landing on Work's first pane (Agents) would resolve, render
  // and look deliberate.
  for (const [hash, tab] of [
    ['#runtimes/catalogue', 'catalogue'],
    ['#history/timeline', 'timeline'],
    ['#activity/timeline', 'timeline'],
    ['#history/counts', 'counts'],
  ] as const) {
    at(hash)
    assert.equal(fromHash().tab, tab, `${hash} lost its tail`)
  }
})

/**
 * `#history/counts` IS NOT AN AGENT CALLED "counts".
 *
 * This is the assertion that catches the collapse's one genuinely dangerous
 * routing case, and it is worth stating on its own because every other test in
 * this file would stay green through it.
 *
 * `history` aliases to `work`, because Timeline went to Work. `work` is the
 * section that owns the agent drawer, and `fromHash` reads a Work tail that
 * matches no tab as a TASK ID -- that fallback is deliberate and is what keeps
 * `#agents/<id>` from the old nav working. Put the two together and, without
 * the MOVED_PANES interception, `#history/counts` opens the agent inspector
 * for a task whose id is "counts": a spinner, then a "not found" panel, on a
 * hash that used to be a working link to the platform's own ledger.
 *
 * MUTATION THIS CATCHES: delete the `'history/counts'` entry from MOVED_PANES,
 * or move the MOVED_PANES lookup below the drawer branch in `fromHash`.
 */
test('a retired pane that moved to a different section is not read as a task id', () => {
  for (const hash of ['#history/counts', '#activity/counts'] as const) {
    at(hash)
    const r = fromHash()
    assert.equal(r.taskId, null, `${hash} opened the agent drawer`)
    assert.equal(r.sectionId, 'admin', `${hash} did not reach Admin`)
    assert.equal(r.tab, 'counts', `${hash} did not reach the Platform counts pane`)
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

/**
 * OV-10. THE AGENT LIST'S TAB AND ITS RECENT STATE ARE ADDRESSES.
 *
 * The Overview's failed-agents item linked to `#work/running` and said
 * "agents · Recent tab", because the tab was component state the hash could
 * not carry -- and the list then opened on whichever tab had rows, which is
 * Live whenever anything runs. The owner's decision (epic #81, OV-10) makes
 * the tab and the Recent tab's state filter part of the address:
 *
 *   #work/running/<live|waiting|recent>
 *   #work/running/recent/<failed|cancelled|succeeded>
 *
 * Read off `route.list` through a cast, so this file compiles against a Route
 * that does not declare the field -- the state the red run was taken in.
 */
function listOf(hash: string): unknown {
  at(hash)
  return (fromHash() as unknown as { list?: unknown }).list ?? null
}

test('OV-10: every list address round-trips through its canonical spelling', () => {
  for (const hash of [
    'work/running/live',
    'work/running/waiting',
    'work/running/recent',
    'work/running/recent/failed',
    'work/running/recent/cancelled',
    'work/running/recent/succeeded',
  ]) {
    at(`#${hash}`)
    const r = fromHash()
    assert.equal(r.taskId, null, `#${hash} opened the agent drawer`)
    assert.equal(r.sectionId, 'work')
    assert.equal(r.tab, 'running')
    assert.equal(canonical(r), hash, `#${hash} is rewritten to something else`)
  }
  assert.deepEqual(listOf('#work/running/recent/failed'), { tab: 'recent', state: 'failed' })
  assert.deepEqual(listOf('#work/running/waiting'), { tab: 'waiting', state: null })
  // Through the alias too, and rewritten to the current spelling.
  at('#agents/running/recent/failed')
  assert.equal(canonical(fromHash()), 'work/running/recent/failed')
})

/**
 * NOTHING UNDER `running/` IS A TASK ID. `fromHash` reads an unmatched Work
 * tail as a task id -- the old nav's `#work/<id>` -- and before OV-10 that is
 * what `#work/running/recent` became: an agent drawer for a task called
 * "running/recent". An address that is not one of the list addresses above
 * lands on the plain list, with no list state and no drawer.
 *
 * MUTATION: parse the list addresses AFTER the unmatched-tail rule, or drop
 * the `running` guard, and `#work/running/bogus` opens a drawer again.
 */
test('OV-10: an address under running/ never opens a drawer, and a mistyped one is the plain list', () => {
  for (const hash of [
    '#work/running/recent',
    '#work/running/bogus',
    '#work/running/live/failed',
    '#work/running/recent/faild',
    '#work/running/recent/dead_lettered',
    '#work/running/',
  ]) {
    at(hash)
    const r = fromHash()
    assert.equal(r.taskId, null, `${hash} opened the agent drawer`)
    assert.equal(r.sectionId, 'work', `${hash} left the Work section`)
    assert.equal(r.tab, 'running', `${hash} left the agent list`)
  }
  for (const hash of [
    '#work/running/bogus',
    '#work/running/live/failed',
    '#work/running/recent/faild',
    '#work/running/recent/dead_lettered',
  ]) {
    assert.equal(listOf(hash), null, `${hash} carried a list state it does not name`)
    assert.equal(canonical(fromHash()), 'work/running', `${hash} is not the plain list`)
  }
  // The drawer's own address is untouched by any of this.
  at('#work/task/task_abc123')
  assert.equal(fromHash().taskId, 'task_abc123')
})

/**
 * #185: THE TIMELINE'S VIEW RIDES ON ITS ADDRESS, AND A QUERY IS NOT A TASK ID.
 *
 * The Timeline writes every filter to the hash (`#work/timeline?span=30d`).
 * Before the query was split off, `timeline?span=30d` matched no tab and the
 * Work fallback read the whole tail as a TASK ID, opening an inspector for an
 * agent called "timeline?span=30d". And `canonical` has to keep the query, or
 * the normalise effect would strip every filter from the address the moment
 * it was written.
 *
 * MUTATION: route on the unsplit hash (the drawer opens); drop the view from
 * `canonical` (the round trip loses the query).
 */
test('#185: #work/timeline?… opens the Timeline with its view, never a drawer, and keeps the query', () => {
  for (const hash of ['#work/timeline?span=30d', '#work/timeline?span=7d&table=1', '#history/timeline?span=90d']) {
    at(hash)
    const r = fromHash()
    assert.equal(r.taskId, null, `${hash} opened the agent drawer`)
    assert.equal(r.sectionId, 'work', `${hash} left Work`)
    assert.equal(r.tab, 'timeline', `${hash} left the Timeline`)
    assert.equal(r.view, hash.slice(hash.indexOf('?') + 1), `${hash} lost its view`)
    assert.equal(canonical(r), `work/timeline?${r.view}`, `${hash} is not written back with its view`)
  }
  // No query, no view: the plain address is unchanged.
  at('#work/timeline')
  assert.equal(fromHash().view ?? null, null)
  assert.equal(canonical(fromHash()), 'work/timeline')
  // And only the Timeline carries one: a query on any other route is dropped.
  at('#work/running?span=30d')
  assert.equal(canonical(fromHash()), 'work/running')
})
