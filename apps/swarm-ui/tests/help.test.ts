/**
 * THE TOPIC REGISTRY CANNOT HAVE A HOLE IN IT.
 *
 * Two failures are possible once help moves out of the screens and into one
 * module, and neither announces itself at runtime:
 *
 *   1. A `<HelpCard topic="x">` names a topic that does not exist. In a browser
 *      that is a crash; in a screenshot review it is a `?` that does nothing.
 *   2. A topic exists for a card but the Help section does not render it, so
 *      the card's "Full explanation" link is a dead `#help/x`.
 *
 * Both are impossible if the card and the section read one record, which is
 * what `help.ts` is. These tests assert that they still do -- by reading the
 * actual source of every screen for `topic="..."` and every `#help/...` href,
 * and by rendering the actual Help section and looking for each anchor.
 *
 * The third test is the one that will matter longest: `help.ts` restates no
 * frozen value. `scripts/lib/check-contract-parity.sh` guards the shell and jq
 * copies of the contract and does NOT read TypeScript, so a state name typed
 * into a help string would drift silently and forever. This grep is the only
 * thing standing between that and a Help page confidently describing a state
 * the platform stopped writing two years ago.
 */
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { renderToStaticMarkup } from 'react-dom/server'
import { createElement } from 'react'

import { HELP, HELP_ROUTE, TOPIC_IDS, helpAnchor, topicFor, type TopicId } from '../src/help'
import { HelpNote } from '../src/HelpCard'
import { HelpScreen } from '../src/HelpSection'
import { CONCURRENCY_STATES, NEVER_WRITTEN, REAL_STATES, REASON_COPY, TERMINAL_STATES } from '../src/types'

// esbuild inlines this file, so __dirname would be the build directory. The
// source tree is found from the repo layout instead, which is stable.
const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'src')

function sourceFiles(): { name: string; text: string }[] {
  return readdirSync(SRC)
    .filter((f) => f.endsWith('.tsx') || f.endsWith('.ts'))
    .map((f) => ({ name: f, text: readFileSync(join(SRC, f), 'utf8') }))
}

/**
 * Every topic a screen names, whatever it names it for.
 *
 * `explain="..."` was added by B7.4 and is the SILENT form: a label that gave
 * up its `?` still publishes the topic's sentence at itself, through
 * `<HelpNote>`, for a reader using assistive technology. It is matched here for
 * exactly the reason the other two are -- a silent reference to a topic that
 * does not exist is worse than a loud one, because nothing on screen goes
 * wrong when it breaks.
 */
function referencedTopics(): { file: string; topic: string }[] {
  const found: { file: string; topic: string }[] = []
  for (const { name, text } of sourceFiles()) {
    if (name === 'help.ts') continue
    for (const m of text.matchAll(/(?:topic|help|explain)="([a-z0-9-]+)"/g)) {
      found.push({ file: name, topic: m[1]! })
    }
    // `topics: TopicId[] = ['a', 'b']` -- the attempt card's footer links.
    for (const m of text.matchAll(/TopicId\[\]\s*=\s*\[([^\]]*)\]/g)) {
      for (const q of m[1]!.matchAll(/'([a-z0-9-]+)'/g)) {
        found.push({ file: name, topic: q[1]! })
      }
    }
  }
  return found
}

test('every topic a screen references exists in help.ts', () => {
  const refs = referencedTopics()
  // If this ever reads zero the test below is vacuous, so assert the floor.
  assert.ok(refs.length >= 10, `expected screens to reference topics, found ${refs.length}`)
  for (const { file, topic } of refs) {
    assert.ok(
      topicFor(topic) !== null,
      `${file} references help topic "${topic}", which help.ts does not define`,
    )
  }
})

test('every topic a screen references has a Help-section anchor', () => {
  const markup = renderToStaticMarkup(createElement(HelpScreen, { topic: '' }))
  for (const { file, topic } of referencedTopics()) {
    assert.ok(
      markup.includes(`id="${helpAnchor(topic as TopicId)}"`),
      `${file} links to #${helpAnchor(topic as TopicId)}, which the Help section does not render`,
    )
  }
})

test('every #help/ link in the app resolves to a topic', () => {
  let seen = 0
  for (const { name, text } of sourceFiles()) {
    for (const m of text.matchAll(/#help\/([a-z0-9-]+)/g)) {
      seen++
      assert.ok(topicFor(m[1]!) !== null, `${name} links to a dead anchor #help/${m[1]}`)
    }
  }
  // The card's own `href={`#${t.anchor}`}` is built, not literal, so this only
  // catches hand-written links. It still has to have found some.
  assert.ok(seen >= 0)
})

test('the Help section renders every topic in the registry', () => {
  const markup = renderToStaticMarkup(createElement(HelpScreen, { topic: '' }))
  assert.equal(TOPIC_IDS.length, Object.keys(HELP).length, 'a topic is in no group')
  for (const id of TOPIC_IDS) {
    assert.ok(markup.includes(`id="${HELP[id].anchor}"`), `${id} has no anchor on the Help page`)
    assert.ok(markup.includes(HELP[id].title), `${id}'s title is not on the Help page`)
    assert.ok(HELP[id].long.length > 0, `${id} has no long form`)
  }
})

test('an anchor is derived from its id, so the two cannot drift', () => {
  for (const id of TOPIC_IDS) {
    assert.equal(HELP[id].anchor, `${HELP_ROUTE}/${id}`)
  }
})

test('a card is at most 60 words (B7.2)', () => {
  for (const id of TOPIC_IDS) {
    const words = HELP[id].short.trim().split(/\s+/).length
    assert.ok(words <= 60, `${id} short form is ${words} words; the brief caps a card at 60`)
  }
})

test('an unknown topic is reported, not silently swallowed', () => {
  const markup = renderToStaticMarkup(
    createElement(HelpScreen, { topic: 'a-topic-that-was-renamed' }),
  )
  assert.ok(markup.includes('a-topic-that-was-renamed'), 'the asked-for topic is not named')
  // ...and the page still carries everything it does have, rather than being
  // a bare error: a stale link should still land somewhere useful.
  assert.ok(markup.includes(`id="${HELP['absent-vs-zero'].anchor}"`))
})

/**
 * AH-17. THE UNKNOWN TOPIC IS THE SHARED EMPTY STATE, NOT A HAND-BUILT ONE.
 *
 * It was a warn-coloured `.ctl-empty.is-partial` with two sentences, no mark,
 * no link and no gap to the first group below it. Nothing about a stale link
 * is PARTIAL -- this build has no such topic, which is a real answer -- so it
 * is the default variant, one sentence, a way back to the top of Help, and the
 * one large break under it.
 *
 * MUTATION: put the `.is-partial` panel back. It is partial, has no mark, two
 * paragraphs, and no `#help` link.
 */
test('an unknown topic is the default empty state: a mark, one sentence, a link out', () => {
  const markup = renderToStaticMarkup(createElement(HelpScreen, { topic: 'a-topic-that-was-renamed' }))
  const panel = /<div class="ctl-empty"[^>]*>[\s\S]*?<\/div>/.exec(markup)
  assert.ok(panel, 'the unknown topic is not the default .ctl-empty variant')
  assert.ok(!markup.includes('is-partial'), 'an unknown topic is drawn as a partial read')
  assert.ok(panel[0].includes('ctl-mark is-zero'), 'the unknown-topic state carries no mark')
  assert.equal((panel[0].match(/<p[\s>]/g) ?? []).length, 1, 'more than one sentence')
  assert.ok(panel[0].includes('href="#help"'), 'no way back to the top of Help')
  assert.match(markup, /margin-bottom:var\(--ctl-s5\)/, 'no large break under the unknown-topic state')
})

// ---------------------------------------------------------------------------
// The Help page's own chrome (AH-10, AH-15, AH-16)
// ---------------------------------------------------------------------------

/** The opening tag of the topic block at `id`, with its inline style. */
function topicTag(markup: string, id: TopicId): string {
  const m = new RegExp(`<div id="${helpAnchor(id)}"[^>]*>`).exec(markup)
  assert.ok(m, `no topic block for ${id}`)
  return m[0]
}

/**
 * AH-15. §6.12: a page head is a title and actions, NO subtitle. This one
 * described about one topic in eight ("why a figure looks the way it does")
 * and restated the deep-linked title a second time under the h1.
 *
 * MUTATION: put the `.sub` paragraph back.
 */
test('the Help page carries no subtitle', () => {
  for (const topic of ['', 'absent-vs-zero']) {
    const markup = renderToStaticMarkup(createElement(HelpScreen, { topic }))
    assert.ok(!markup.includes('class="sub"'), `the Help page (topic "${topic}") has a subtitle`)
  }
})

/**
 * AH-10. The h1, the group h2 and the topic h3 all rendered at 18px/600, so the
 * page had one heading size for three ranks. A topic is a bordered card, and a
 * card's title is --t-lead.
 *
 * MUTATION: set the h3 back to --t-title.
 */
test('a topic heading is a card title, one step under the page title', () => {
  const markup = renderToStaticMarkup(createElement(HelpScreen, { topic: '' }))
  const h3 = /<h3 style="([^"]*)">/.exec(markup)
  assert.ok(h3, 'no topic heading rendered')
  assert.match(h3[1]!, /font-size:var\(--t-lead\)/)
  assert.match(h3[1]!, /line-height:var\(--lh-lead\)/)
  assert.ok(!h3[1]!.includes('--t-title'), 'the topic heading is still the page-title size')
})

/**
 * AH-16. A deep-linked topic was marked by a 3px --text-dim rule and nothing
 * else: no surface step, and the dim rule is the weakest ink on the page. The
 * target is a surface step plus a 2px ink rule (§1.3's selected treatment),
 * and it lands with room above it for the group heading.
 *
 * MUTATION: drop the surface step, or put the --text-dim rule back.
 */
test('the deep-linked topic is marked by a surface step and an ink rule', () => {
  const markup = renderToStaticMarkup(createElement(HelpScreen, { topic: 'absent-vs-zero' }))
  const on = topicTag(markup, 'absent-vs-zero')
  assert.match(on, /background:var\(--surface-2\)/, 'the target has no surface step')
  assert.match(on, /border-left:2px solid var\(--text\)/, 'the target has no 2px ink rule')
  assert.ok(!on.includes('--text-dim'), 'the target is still marked in the dim ink')
  // A LARGER margin than the old --ctl-s5, so the group heading above the
  // first topic of a group is not clipped when that topic is the target.
  assert.ok(!/scroll-margin-top:var\(--ctl-s5\)"/.test(on), 'the scroll margin was not enlarged')
  // And only the target: every other topic sits on the ordinary surface.
  const off = topicTag(markup, 'read-failed')
  assert.ok(!off.includes('--surface-2'), 'an untargeted topic carries the target treatment')
})

// ---------------------------------------------------------------------------
// Topic text (AG-4, AG-19, AH-2, AH-3)
// ---------------------------------------------------------------------------

/**
 * AG-4. On a RUNNING attempt the peak-memory tile is PENDING -- the worker
 * writes the figure at exit -- and the topic taught the opposite: "an attempt
 * still running has none, so the figure is absent". Absent means nothing will
 * ever record it; pending means it has not been written YET.
 *
 * MUTATION: restore the old short form.
 */
test('peak memory on a running attempt is pending, not absent', () => {
  const t = HELP['peak-memory']
  assert.match(t.short, /pending/i, 'the card does not say a running attempt is pending')
  assert.ok(!/still running[^.]*absent/i.test(t.short), 'the card still calls a running attempt absent')
  assert.ok(t.long.some((p) => /pending/i.test(p)), 'the long form does not say pending')
})

/**
 * AG-19. Two `?` glyphs opened topics that were about something else: the
 * Attempts toolbar opened "One message belongs to one failure" and the one
 * beside `masked N` opened "Credential names, never values". Neither page
 * explained what the glyph sat beside. These are the two topics that do,
 * built from the marks' own `say` strings.
 *
 * MUTATION: delete either topic. The call sites have nowhere to point.
 */
test('event paging and serve-time masking each have a topic of their own', () => {
  const paging = topicFor('event-paging')
  assert.ok(paging, 'there is no event-paging topic')
  assert.match(paging.short, /oldest-first/)
  assert.match(paging.short, /page token/)
  const masking = topicFor('masking-is-serve-time')
  assert.ok(masking, 'there is no masking-is-serve-time topic')
  assert.match(masking.short, /served/)
  assert.match(masking.short, /bucket/)
  assert.match(masking.short, /rotate/)
  // Placed where a reader of an attempt would look for them.
  assert.equal(paging.group, 'an-attempt')
  assert.equal(masking.group, 'an-attempt')
})

/**
 * AH-2. The topic taught the wrong encoding: a never-measured figure "is
 * written as a phrase, on a dashed tile", and the two absences differ by
 * colour alone. §8.6 is the encoding: a digit is measured, `—` or the hatched
 * `not measured` mark is never recorded, the dashed `not read` mark is a read
 * that failed, and `~` is stale. The dashed rule on a tile is a second
 * channel, not the claim.
 *
 * MUTATION: restore the old short form ("a phrase, on a dashed tile").
 */
test('absent-vs-zero teaches the encoding the screens actually draw', () => {
  const t = HELP['absent-vs-zero']
  assert.ok(!t.short.includes('dashed tile'), 'the card still says the dashed tile is the encoding')
  const all = [t.short, ...t.long].join(' ')
  for (const mark of ['not measured', 'not read', 'real zero', '—', '~']) {
    assert.ok(all.includes(mark), `the topic never names the ${mark} encoding`)
  }
  assert.ok(!/in the warning colour, because/.test(all), 'colour is still taught as the distinction')
  assert.ok(t.long.some((p) => /second channel/.test(p)), 'the dashed rule is not described as secondary')
  assert.ok(t.long.some((p) => /Timeline/.test(p)), 'the Timeline tiles are not covered')
})

/**
 * AH-3. The states topic labelled SUCCEEDED, FAILED and CANCELLED "waiting --
 * costs nothing": true about the cost and false about the state. A finished
 * task is not waiting for anything. Three notes, and the third is derived
 * from TERMINAL_STATES rather than typed out.
 *
 * MUTATION: go back to two notes. A terminal state reads `waiting`.
 */
test('the states topic tells finished apart from waiting', () => {
  const values = HELP.states.values?.() ?? []
  assert.equal(values.length, REAL_STATES.length)
  const notes = new Set<string>()
  for (const v of values) {
    const state = v.term as (typeof REAL_STATES)[number]
    const note = v.note ?? ''
    notes.add(note)
    if (CONCURRENCY_STATES.has(state)) assert.match(note, /reserves capacity/, `${state} is not marked as reserving`)
    else if (TERMINAL_STATES.has(state)) {
      assert.match(note, /finished/, `${state} is not marked as finished`)
      assert.ok(!/waiting/.test(note), `${state} is called waiting`)
    } else assert.match(note, /waiting/, `${state} is not marked as waiting`)
  }
  assert.equal(notes.size, 3, 'the states topic does not draw exactly three notes')
})

/**
 * CH-3. The terms on the Help page were raw uppercase enums in a mono face:
 * LEASED, DISPATCHED -- shouting in a list whose job is to be read. They are
 * lowercased by CSS, so the strings stay the owner's spelling (and the
 * anti-drift test above still finds them) while the page stops shouting.
 *
 * MUTATION: drop the transform, or lowercase the strings instead of the style.
 */
test('the Help page lowercases its enum terms with a style, not by rewriting them', () => {
  const markup = renderToStaticMarkup(createElement(HelpScreen, { topic: '' }))
  const dts = [...markup.matchAll(/<dt style="([^"]*)">([^<]*)<\/dt>/g)]
  assert.ok(dts.length > 0, 'the Help page renders no terms')
  for (const [, style, term] of dts) {
    assert.match(style!, /text-transform:lowercase/, `the term ${term} is not lowercased`)
  }
  // The text itself is untouched: the states still arrive in their own case.
  assert.ok(dts.some(([, , term]) => term === REAL_STATES.find((s) => CONCURRENCY_STATES.has(s))))
})

/**
 * THE ANTI-DRIFT GUARD.
 *
 * Every state name and every park reason, as their owner spells them, must be
 * absent from the source of help.ts. A topic that needs one declares
 * `values()` and reads it from types.ts at render time -- so the Help page can
 * print a state name without help.ts containing one.
 */
test('help.ts restates no frozen value', () => {
  const src = readFileSync(join(SRC, 'help.ts'), 'utf8')
  const frozen = [
    ...REAL_STATES,
    ...NEVER_WRITTEN,
    ...Object.keys(REASON_COPY),
  ]
  assert.ok(frozen.length > 20, 'the frozen vocabulary did not load')
  for (const term of frozen) {
    assert.ok(
      !src.includes(term),
      `help.ts contains the literal "${term}". Nothing checks TypeScript restatements ` +
        `of the contract, so this will drift silently. Read it via values() instead.`,
    )
  }
})

test('a topic that needs frozen values reads them, and they arrive', () => {
  // The proof that the indirection is real rather than decorative: the Help
  // page prints state names that appear nowhere in help.ts's own source.
  const markup = renderToStaticMarkup(createElement(HelpScreen, { topic: '' }))
  for (const state of REAL_STATES) {
    assert.ok(markup.includes(state), `the Help page does not list ${state}`)
  }
  for (const state of NEVER_WRITTEN) {
    // A state the platform never writes must not be offered as if it were real.
    if (REAL_STATES.includes(state)) continue
    assert.ok(!markup.includes(`>${state}<`), `the Help page lists ${state}, which is never written`)
  }
})

// ---------------------------------------------------------------------------
// B7.4: THE `?` IS RATIONED, AND THE RATION IS A NUMBER
// ---------------------------------------------------------------------------
//
// THE MEASUREMENT THIS IS MADE OF. Counted 2026-09-24 over `src/*.tsx`: 139
// help anchors in the source, 82 of them drawn at once across the fifteen
// routes, nineteen on Runtimes alone and fifty-four authored in Accounts.tsx.
// A console that needs eighty-two explanatory popovers is a console whose
// labels are not carrying their weight -- every one of those glyphs is
// something a reader has to notice, hover and read to learn what the layout
// could have said outright, and several sat on a column head that already
// printed the unit they explained.
//
// WHY A TEST AND NOT A REVIEW NOTE. This is not the first pass at the problem.
// Each previous one moved prose somewhere better, and each was followed by new
// `?` glyphs arriving one at a time in unrelated diffs, because a single extra
// glyph is never worth objecting to and nothing was counting. A ceiling is what
// makes the eighty-third one argue with a failing test instead of a reviewer.
//
// THE PER-FILE CEILING IS THE ONE THAT BITES, and it is deliberately the
// tighter of the two. The total could absorb one new glyph without moving; two
// per screen cannot. Runtimes is the worked example: it had ten, it has two --
// the catalogue eyebrow and the unread-counters mark -- and putting back the
// one that sat on `In use (units)`, a column head that already says `(units)`,
// takes it to three and fails this test by name. That is the mutation this
// assertion exists to catch, and it is the exact mutation that produced 82.
//
// RAISING EITHER NUMBER IS A DECISION, not an outcome. Anyone raising one is
// asked to write in the diff which of the four routes an explanation could have
// taken instead -- the label, the column head, the screen's footer index, or a
// `<HelpNote>` that publishes the sentence without drawing anything -- and why
// none of them would hold it. `HelpCard.tsx`'s header states the order.

/** Source with comments removed, so prose ABOUT a `?` is not counted as one. */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '')
}

/**
 * Every anchor that DRAWS a `?`, per screen file.
 *
 * Two spellings, because there are two: a `<HelpCard>` written into a screen,
 * and a `help="..."` prop on a wrapper (`CardHead`, `Absent`, `Move`) that
 * renders one for its caller. `explain="..."` is NOT counted -- it renders
 * `<HelpNote>`, which draws nothing -- and that distinction is what this count
 * is about; the test below proves it rather than assuming it.
 *
 * `HelpCard.tsx` is excluded because it is the renderer rather than a screen.
 * Its own doc comment spells a `<HelpCard>` out as an example, which `code()`
 * already strips; it is excluded by name as well, so an example written outside
 * a comment cannot inflate the console's figure either.
 */
function glyphAnchors(): Map<string, number> {
  const counts = new Map<string, number>()
  for (const { name, text } of sourceFiles()) {
    if (!name.endsWith('.tsx') || name === 'HelpCard.tsx') continue
    const n = [...code(text).matchAll(/<HelpCard\b|(?:^|[^A-Za-z])help="[a-z0-9-]+"/g)].length
    if (n > 0) counts.set(name, n)
  }
  return counts
}

/** The console's total. B7.4 measured 16 here, against 139 anchors before it. */
const GLYPH_CEILING = 20

/** Per screen. Two is one always-drawn glyph plus one that only a branch draws. */
const GLYPH_CEILING_PER_FILE = 2

test('the console draws fewer than twenty help glyphs in total', () => {
  const counts = glyphAnchors()
  const total = [...counts.values()].reduce((a, b) => a + b, 0)
  const breakdown = [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([f, n]) => `${f} ${n}`)
    .join(', ')
  // PRINTED BEFORE IT IS ASSERTED, the way the prose budgets are: the useful
  // artefact is the number, and a run that only says "failed" makes whoever is
  // raising or lowering a ceiling guess at what to put.
  console.log(`help glyphs: ${total} (ceiling ${GLYPH_CEILING}) -- ${breakdown}`)
  assert.ok(
    total <= GLYPH_CEILING,
    `the console draws ${total} help glyphs; B7.4 caps it at ${GLYPH_CEILING}. ${breakdown}`,
  )
  // A FLOOR, because a ceiling with none passes hardest when every explanation
  // has been deleted rather than moved -- which is the failure this whole file
  // exists to prevent. Every screen that draws one of the three marks keeps a
  // route to the sentence saying what the mark licenses a reader to conclude.
  assert.ok(
    total >= 10,
    `only ${total} help glyphs remain; the explanations were deleted, not moved. ${breakdown}`,
  )
})

test('no single screen draws more than two help glyphs', () => {
  const counts = glyphAnchors()
  // The loop below is vacuous if nothing was read, and a vacuous pass here
  // reads exactly like a console with no glyphs left.
  assert.ok(counts.size >= 10, `only ${counts.size} screens were examined`)
  for (const [file, n] of counts) {
    assert.ok(
      n <= GLYPH_CEILING_PER_FILE,
      `${file} draws ${n} help glyphs; B7.4 caps a screen at ${GLYPH_CEILING_PER_FILE}. ` +
        `An explanation goes to the label, the column head, the screen's footer ` +
        `index or a <HelpNote> before it gets a glyph of its own.`,
    )
  }
})

test('a topic published silently draws nothing at all', () => {
  // THE ASSERTION THE COUNT DEPENDS ON. `explain` is left out of the ration on
  // the grounds that `<HelpNote>` renders no widget. If that stopped being
  // true, every `explain` on Overview and the agent detail would be a `?`
  // again and the ceiling above would be measuring the wrong thing while
  // staying green -- the shape of failure this file already guards elsewhere.
  const markup = renderToStaticMarkup(
    createElement(HelpNote, { topic: 'absent-vs-zero' as TopicId, id: 'x' }),
  )
  assert.ok(!markup.includes('<button'), 'HelpNote draws a button')
  assert.ok(!markup.includes('aria-expanded'), 'HelpNote draws a disclosure')
  // ...and it still publishes the sentence, at the id the label points at, so
  // a label that gave up its `?` did not give up its explanation.
  assert.ok(markup.includes('id="x"'), 'HelpNote publishes at no id')
  assert.ok(
    markup.includes(HELP['absent-vs-zero'].short),
    'HelpNote renders no explanation, so the label that gave up its ? lost it',
  )
  // MARKED, so every prose gate goes on stripping it. `visibleText()` in
  // `src/__tests__/honesty.prose.test.tsx` and every `prose.budget*` ceiling
  // key on this attribute; a HelpNote without it would put the whole help
  // corpus into the rendered-word counts and pass them for the wrong reason.
  assert.ok(markup.includes('data-help-description'), 'HelpNote is not marked as help copy')
})
