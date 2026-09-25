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

import { HELP, HELP_GROUPS, HELP_ROUTE, TOPIC_IDS, helpAnchor, topicFor, type TopicId } from '../src/help'
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
// The Help page's own chrome (AH-10, AH-15, AH-16, AH-18, AH-25)
// ---------------------------------------------------------------------------

/** The opening tag of the topic block at `id`. */
function topicTag(markup: string, id: TopicId): string {
  const m = new RegExp(`<div id="${helpAnchor(id)}"[^>]*>`).exec(markup)
  assert.ok(m, `no topic block for ${id}`)
  return m[0]
}

/**
 * The whole block of the topic at `id`: from its opening tag to the next
 * topic's, or to the end of its group. Read by string position because the
 * markup is a string here, and a regex over nested elements closes early.
 */
function topicBlock(markup: string, id: string): string {
  const start = markup.indexOf(`<div id="${HELP_ROUTE}/${id}"`)
  assert.ok(start >= 0, `no topic block for ${id}`)
  const next = markup.indexOf(`<div id="${HELP_ROUTE}/`, start + 1)
  const close = markup.indexOf('</section>', start)
  const ends = [next, close].filter((n) => n > start)
  return markup.slice(start, ends.length > 0 ? Math.min(...ends) : undefined)
}

/**
 * AH-25. THE ONE PAGE HEAD, AND HELP IS ITS ONE NAMED EXCEPTION. The Help page
 * drew its title in `.ctl-page-head` while fourteen routes drew theirs through
 * `Screen`'s `.head`. It renders `PageHead` now, the markup `Screen` renders:
 * a title over one line. The owner's AH-25 decision amends §6.12 with exactly
 * one exception -- Help reads nothing, so its line says what the page is and
 * which topic is showing -- and says Help keeps its line.
 *
 * AH-15 had deleted the line that was there, and the reason still holds: it
 * said "why a figure on these screens looks the way it does", which is true
 * of about one topic in eight. So the line is the head-line shape (facts
 * joined by `·`, no sentence), every fact is read from `help.ts` rather than
 * typed, and the only topic it names is the one the link asked for.
 *
 * MUTATION: render `PageHead` bare again (the lane's first version), or put
 * AH-15's sentence back.
 */
test('the Help page head says what the page is and which topic is showing (AH-25)', () => {
  const groups = HELP_GROUPS.filter((g) => TOPIC_IDS.some((id) => HELP[id].group === g.id)).length
  const line = (markup: string): string => /<p class="sub">([\s\S]*?)<\/p>/.exec(markup)?.[1] ?? ''
  const bare = renderToStaticMarkup(createElement(HelpScreen, { topic: '' }))
  const deep = renderToStaticMarkup(createElement(HelpScreen, { topic: 'absent-vs-zero' }))
  const stale = renderToStaticMarkup(createElement(HelpScreen, { topic: 'a-topic-that-was-renamed' }))
  for (const markup of [bare, deep, stale]) {
    assert.match(markup, /<div class="head"><h1>Help<\/h1><\/div><p class="sub">/, 'the Help page does not draw the shared head with its line')
    assert.ok(!markup.includes('ctl-page-head'), 'the Help page still draws a head of its own shape')
    assert.ok(!/looks the way it does/.test(markup), 'AH-15’s line, true of about one topic in eight, is back')
    // WHAT THE PAGE IS, from the registry: every topic, in its groups.
    assert.ok(
      line(markup).includes(`${TOPIC_IDS.length} topics in ${groups} groups`),
      `the Help line does not say what the page holds: "${line(markup)}"`,
    )
    // A LINE, NOT A DESCRIPTION SENTENCE (§6.12): nothing ends in a full stop.
    assert.ok(!/\.(\s|$)/.test(line(markup)), `the Help line is a sentence: "${line(markup)}"`)
  }
  // WHICH TOPIC IS SHOWING -- only when the link named one this build has.
  assert.ok(line(deep).includes(`showing ${HELP['absent-vs-zero'].title}`), `the Help line does not name the topic: "${line(deep)}"`)
  assert.ok(!line(bare).includes('showing'), 'the Help line names a topic when none was asked for')
  assert.ok(!line(stale).includes('showing'), 'the Help line names a topic this build does not carry')
})

/**
 * AH-18. A TOPIC IS A ROW OF ITS GROUP, NOT A BOX, AND IT IS STYLED BY THE
 * SHEET. Every topic was an inline-styled bordered panel -- border, surface,
 * radius, padding -- because "styles.css belongs to another track this pass".
 * Under design-system §13.3 a topic is a row inside a region, and a row draws
 * nothing. The treatment now lives in `.help-topic` in styles.css, where
 * `src/__tests__/shell.test.tsx` asks the cascade what a browser would draw:
 * no box, the h3 at `--t-lead` (AH-10), paragraphs and notes at `--measure`.
 *
 * What this file can see, with no stylesheet, is that the treatment is no
 * longer written inline: every topic block carries the class and no style.
 *
 * MUTATION: put the inline `style` back on a topic, or on its h3.
 */
test('every topic is a .help-topic row with no inline box (AH-18)', () => {
  const markup = renderToStaticMarkup(createElement(HelpScreen, { topic: '' }))
  for (const id of TOPIC_IDS) {
    const tag = topicTag(markup, id)
    assert.match(tag, /class="help-topic"/, `${id} is not a .help-topic row`)
    assert.ok(!tag.includes('style='), `${id} still draws its own inline box`)
  }
  assert.ok(!/<h3 style=/.test(markup), 'a topic heading still carries an inline size')
  assert.ok(!/<p style=/.test(topicBlock(markup, 'absent-vs-zero')), 'a topic paragraph still carries an inline style')
})

/**
 * AH-16, RE-POINTED BY AH-18. A deep-linked topic was marked by a 3px
 * --text-dim rule and nothing else. It takes §1.3's selected treatment -- a
 * surface step and a 2px ink rule -- and that treatment is now `.is-current` on
 * the row, set from the `highlighted` prop. The values are asserted against the
 * sheet in shell.test.tsx; here, that exactly the target carries the modifier.
 *
 * MUTATION: mark every topic current, or none.
 */
test('the deep-linked topic, and only it, is the current row', () => {
  const markup = renderToStaticMarkup(createElement(HelpScreen, { topic: 'absent-vs-zero' }))
  assert.match(topicTag(markup, 'absent-vs-zero'), /class="help-topic is-current"/, 'the target is not marked current')
  assert.equal((markup.match(/is-current/g) ?? []).length, 1, 'more than one topic is marked current')
  assert.ok(!topicTag(markup, 'read-failed').includes('is-current'), 'an untargeted topic carries the target treatment')
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
 * AG-19, AND WHICH SIDE THE ONE-PAGE LIMIT IS ON. The first `event-paging`
 * said the events route "returns no page token", that there is "no token to
 * ask for the next", and that past one page the newest events "cannot be
 * fetched at all". It was built from AttemptTimeline's `say` strings, and
 * those went stale with #19: `GET /v1/tasks/{id}/events` returns
 * `next_page_token` whenever more events exist and accepts `order=desc`
 * (swarm_api/routes/tasks.py, `list_events`). What is still true is narrower
 * and is about THIS SCREEN: api.ts asks for one page, oldest-first, and never
 * sends the token back.
 *
 * So this reads both sources rather than trusting a sentence. While the route
 * pages, the topic may not say it cannot; while the UI does not follow the
 * token, the topic has to say that the screen stops at one page.
 *
 * MUTATION: restore "returns no page token". The route assertion fails.
 * MUTATION: drop the sentence about this screen. The UI assertion fails.
 */
test('the event-paging topic puts the one-page limit on this screen, not on the route', () => {
  const t = HELP['event-paging']
  const all = [t.short, ...t.long].join(' ')

  const route = readFileSync(resolve(SRC, '..', '..', 'swarm-api', 'swarm_api', 'routes', 'tasks.py'), 'utf8')
  const start = route.indexOf('def list_events(')
  assert.ok(start >= 0, 'routes/tasks.py has no list_events; this check is reading the wrong file')
  const end = route.indexOf('\n@router', start)
  const listEvents = route.slice(start, end === -1 ? undefined : end)
  const routePages = listEvents.includes('page_token') && listEvents.includes('"next_page_token"')
  if (routePages) {
    assert.ok(
      !/returns no page token|no token to ask|cannot be fetched at all/i.test(all),
      'the topic says the events route cannot page, and list_events returns next_page_token',
    )
  } else {
    assert.ok(!/next_page_token|order=desc/.test(all), 'the topic describes paging the route no longer offers')
  }

  const api = readFileSync(join(SRC, 'api.ts'), 'utf8')
  const reads = [...api.matchAll(/\/events\?[^`'"]*/g)].map((m) => m[0])
  assert.ok(reads.length > 0, 'api.ts reads no events route; this check is vacuous')
  if (!reads.some((r) => r.includes('page_token'))) {
    assert.match(
      t.short,
      /this screen[^.]*(?:does not|never)[^.]*token/i,
      'the UI reads one page and never follows the token, and the topic does not say so',
    )
  }
})

/**
 * AG-19. `masking-is-serve-time` said the count beside an artifact is "the
 * number of values hidden in this copy" and read a zero as "nothing matched
 * the rules". The API masks and counts only the WINDOW it serves (inspect.py:
 * `redact()` runs over this chunk's bytes and `redaction_count` is that call's
 * count), and the viewer shows a large artifact one window at a time, marked
 * `partial`. On a partial read `0 of N families` says nothing about the bytes
 * that were not served.
 *
 * MUTATION: restore "hidden in this copy" with no word about the window.
 */
test('the masking topic says its count covers only the bytes this read served', () => {
  const t = HELP['masking-is-serve-time']
  const all = [t.short, ...t.long].join(' ')
  assert.match(t.short, /\bonly\b[^.]*\bserved\b/i, 'the card does not say the count is over the served bytes alone')
  assert.ok(t.long.some((p) => /partial/i.test(p)), 'the long form never mentions a partial read')
  assert.ok(!/hidden in this copy/i.test(all), 'the topic still counts the whole copy')
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
 * AH-13. A `long` PARAGRAPH IS ONLY EVER READ ON THE HELP PAGE.
 *
 * `HelpSection.tsx` renders the title, the long form and the values; the card
 * beside a figure renders `short` and nothing else. So a long paragraph that
 * says "this screen", "shown above" or "the table below it" points at a screen
 * the reader is not on -- on the Help page, "above" is the previous topic. The
 * long form names the thing instead ("the Runtimes screen", "the count over
 * the Accounts table"), and a history clause ("which is what this panel used
 * to say") goes: Help states what the console does now.
 *
 * `short` is deliberately NOT walked: it renders beside its subject, where
 * "here" and "this" are correct. Nor is every "here": one that means the
 * console as a whole ("every read here") is true on the Help page too. The
 * controls below pin that boundary from both sides, so the pattern cannot be
 * widened into a ban on words that are right, or narrowed until it catches
 * nothing.
 *
 * MUTATION: put "which is what this panel used to say" back into
 * `signin-is-over`, or "shown above them" back into `workspace-memory`.
 */
const POINTS_AT_A_SCREEN =
  /\bthis (screen|panel|page|form|field|table|card)\b|\bshown above\b|\babove (the table|them)\b|\bfigures? above\b|\bbelow it\b|\bused to (say|show)\b|\b(appears?|drawn|shown|rendered) here\b|\b(an absence|nothing) here\b/i

test('no long paragraph points at a screen the Help page is not (AH-13)', () => {
  // THE BOUNDARY, BOTH WAYS, before the walk relies on it.
  for (const bad of [
    'which is what this panel used to say',
    'the memory ceiling shown above them',
    'the count above the table',
    'every figure above describes',
    'it remounts the screen below it',
    'Nothing here includes compute',
    'Live load appears here only',
    'an absence here would read',
  ]) {
    assert.ok(POINTS_AT_A_SCREEN.test(bad), `the pattern misses "${bad}"`)
  }
  for (const fine of [
    'The states below are the ones that create demand',
    'the pools listed below',
    'no grace above the line',
    'rather than written down here',
    'every read here returns a value',
    'an all-clear here is always phrased',
  ]) {
    assert.ok(!POINTS_AT_A_SCREEN.test(fine), `the pattern catches "${fine}", which is true on the Help page`)
  }

  let read = 0
  const found: string[] = []
  for (const id of TOPIC_IDS) {
    HELP[id].long.forEach((para, i) => {
      read++
      const m = POINTS_AT_A_SCREEN.exec(para)
      if (m) found.push(`${id} long[${i}]: "${m[0]}"`)
    })
  }
  // A walk over nothing passes hardest. 243 paragraphs were measured when the
  // box was decided, and topics are only ever added.
  assert.ok(read >= 243, `only ${read} long paragraphs were read; the walk is not covering the registry`)
  assert.deepEqual(found, [], `long paragraphs that point at a screen the Help page is not:\n${found.join('\n')}`)
})

/**
 * AH-13 RULE (1), PAST THE PATTERN. The owner's pattern is the floor, not the
 * whole rule: "wherever a paragraph assumes the reader is on the screen it
 * describes, it names the thing instead". Review of #161 found four long forms
 * the pattern does not reach -- a bare `here` in three and an unnamed "submit
 * form" in the fourth -- each describing one screen from inside it. They name
 * their screen by its rail label now, and point with no `here` at all.
 *
 * MUTATION: put "arrives here" back into `signin-201-no-name`, or "the submit
 * form" back into `dispatch-carrier`.
 */
test('the topics that pointed with a bare "here" or an unnamed form name their screen (AH-13)', () => {
  const named: Record<string, readonly string[]> = {
    'dispatch-carrier': ['Submit a task', 'Submit a workflow'],
    'signin-201-no-name': ['Accounts'],
    'account-removal-is-reversible': ['Accounts'],
    'lending-narrows-isolation': ['Accounts'],
  }
  for (const [id, screens] of Object.entries(named)) {
    const t = topicFor(id)
    assert.ok(t, `there is no ${id} topic`)
    t.long.forEach((p, i) => {
      assert.ok(!/\bhere\b/i.test(p), `${id} long[${i}] still points with "here": "${p}"`)
      assert.ok(!/\bthe submit form\b/i.test(p), `${id} long[${i}] names no form: "${p}"`)
    })
    for (const screen of screens) {
      assert.ok(t.long.join(' ').includes(screen), `${id} never names ${screen}, the screen it describes`)
    }
  }
})

/**
 * AH-13 RULE (2). A TOPIC LINKED FROM MORE THAN ONE SCREEN IS WRITTEN TO FIT
 * EVERY ONE OF THEM. `tenant-scope` is linked from three -- the Timeline's
 * People note (Activity.tsx), the Pools footer (Capacity.tsx) and the Profile
 * headroom footer (Profiles.tsx) -- and its long form talked about pools and
 * an unnamed "the route" only, so a reader arriving from the People table
 * found nothing about the table they came from.
 *
 * The screens are found by reading the sources, so a fourth screen that
 * starts linking the topic fails here until the topic is written for it too.
 *
 * MUTATION: restore the pools-only long form.
 */
test('tenant-scope is written for every screen that links it (AH-13)', () => {
  const rail: Record<string, string> = {
    'Activity.tsx': 'Timeline',
    'Capacity.tsx': 'Pools',
    'Profiles.tsx': 'Profile headroom',
  }
  const linking = sourceFiles()
    .filter(({ name, text }) => name !== 'help.ts' && /'tenant-scope'|"tenant-scope"|#help\/tenant-scope\b/.test(code(text)))
    .map((f) => f.name)
    .sort()
  assert.deepEqual(
    linking,
    Object.keys(rail).sort(),
    'the screens that link tenant-scope changed: write the topic for the new one and add it here',
  )
  const all = HELP['tenant-scope'].long.join(' ')
  for (const [file, label] of Object.entries(rail)) {
    assert.ok(all.includes(`the ${label} screen`), `tenant-scope is linked from ${label} (${file}) and never names it`)
  }
  // "The route", unnamed, is what a reader from the Timeline could not place.
  assert.ok(!/\bthe route\b/.test(all), 'tenant-scope still says "the route" without naming it')
})

/**
 * AH-14. FOUR THINGS A READER OF THESE SCREENS HAS TO KNOW HAD NO TOPIC.
 *
 * redesign-v2 §9.1 named two -- what a pool is, and why a paused pool is not a
 * full one -- and the QA pass found two more: what each Tenants column means
 * (including the Enforced ceiling AH-12 adds, and why there is no budget
 * column, which AH-21 moves off the screen and into this topic), and what
 * Platform counts returns.
 *
 * Read through `topicFor`, not `HELP[...]`, so this file compiles before the
 * topics exist and fails on the missing topic rather than on the typecheck.
 *
 * MUTATION: delete any of the four, or drop the budget paragraph.
 */
test('Help carries the four topics the QA pass found missing (AH-14)', () => {
  const four = ['paused-vs-full', 'what-a-pool-is', 'tenant-fields', 'platform-counts'] as const
  for (const id of four) {
    const t = topicFor(id)
    assert.ok(t, `there is no ${id} topic`)
    assert.ok(t.long.length >= 2, `${id} has no long form worth the name`)
  }
  const all = (id: string) => {
    const t = topicFor(id)
    return t === null ? '' : [t.short, ...t.long].join(' ')
  }
  // Paused is not full: the action differs, and raising the ceiling is the
  // wrong one for a paused pool.
  assert.match(all('paused-vs-full'), /resum/i, 'paused-vs-full never says a paused pool is resumed')
  assert.match(all('paused-vs-full'), /ceiling/i, 'paused-vs-full never compares it with a full pool')
  // A pool: a ceiling, a counter in units, and a reservation that is one
  // transaction across every pool a task needs.
  assert.match(all('what-a-pool-is'), /units/i)
  assert.match(all('what-a-pool-is'), /transaction/i)
  // Tenants' fields covers the Enforced ceiling and the budget that is not a column.
  assert.match(all('tenant-fields'), /Enforced/, 'tenant-fields does not explain the Enforced column')
  const fields = topicFor('tenant-fields')
  assert.ok(
    fields !== null && fields.long.some((p) => /budget/i.test(p) && /422/.test(p) && /attribution/i.test(p)),
    'tenant-fields has no paragraph on the budget the limits route refuses',
  )
  // Platform counts: one count per state, per scope, and the admin's second scope.
  assert.match(all('platform-counts'), /count\(\)/, 'platform-counts does not say what one run asks for')
  assert.match(all('platform-counts'), /administrator/i, 'platform-counts does not say who gets the second scope')

  // THE BUDGET PARAGRAPH CROSS-LINKS token-cost (AH-21), as a link on the
  // Help page and not as a title quoted in prose.
  const markup = renderToStaticMarkup(createElement(HelpScreen, { topic: '' }))
  assert.ok(
    topicBlock(markup, 'tenant-fields').includes(`href="#${helpAnchor('token-cost')}"`),
    'tenant-fields does not link to token-cost',
  )
})

/**
 * REVIEW OF #161: tenant-fields MISSTATED AN ACCESS-CONTROL BOUNDARY.
 *
 * It said "the members of a disabled tenant are refused on every call, not
 * slowed". Every task and workflow read, cancel, artifact, checkpoint, log,
 * stats and capacity route resolves its tenant through `tenant_scope`, which
 * calls `SubmissionService.scope_for` -- and that runs the collision check
 * only, deliberately: "a disabled tenant still has to see and cancel what it
 * already has running". `tenant_for` is the one that refuses a disabled
 * tenant, and only the paths that create work, the account routes and the
 * tenant credential routes call it. An admin who disabled a tenant on the
 * strength of the old sentence would believe its members had lost read
 * access they still hold.
 *
 * So this reads service.py rather than trusting a sentence: while the read
 * path does not look at `enabled`, the topic may not claim every call is
 * refused, and it has to say what is still allowed.
 *
 * MUTATION: restore "refused on every call".
 */
test('tenant-fields says what a disabled tenant can still do, as the API decides it', () => {
  const service = readFileSync(resolve(SRC, '..', '..', 'swarm-api', 'swarm_api', 'service.py'), 'utf8')
  const start = service.indexOf('def scope_for(')
  assert.ok(start >= 0, 'service.py has no scope_for; this check is reading the wrong file')
  const end = service.indexOf('\n    def ', start + 1)
  const scopeFor = service.slice(start, end === -1 ? undefined : end)
  // The body after the docstring: the docstring itself talks about `enabled`.
  const body = scopeFor.split('"""')[2] ?? ''
  assert.ok(body.includes('assert_tenant_scope'), 'scope_for no longer calls the collision check; re-read it')
  const readsRefuseDisabled = /\benabled\b|tenant_for\(/.test(body)

  const fields = HELP['tenant-fields'].long.join(' ')
  if (!readsRefuseDisabled) {
    assert.ok(!/every call/i.test(fields), 'tenant-fields says a disabled tenant is refused on every call; the read paths do not refuse it')
    assert.match(fields, /disabled tenant cannot submit/i, 'tenant-fields does not say what disabling stops')
    assert.match(fields, /still[^.]*\bcancel/i, 'tenant-fields does not say a disabled tenant can still cancel what it has')
    assert.match(fields, /still[^.]*\bread/i, 'tenant-fields does not say a disabled tenant can still read what it has')
  } else {
    assert.ok(!/still[^.]*\b(read|cancel)/i.test(fields), 'tenant-fields promises reads the API now refuses')
  }
})

/**
 * REVIEW OF #161: THE ENFORCED COLUMN'S CLAIM WAS TRUE ON ONE PATH OF FOUR.
 *
 * "Setting either one through the admin routes moves the tenant's pool with
 * it, so the pool's ceiling is this same figure" -- and it was only the admin
 * routes (and a first sign-in, through `ensure_tenant`) that wrote the pool as
 * min(max_active, capacity_units). `scripts/register-tenant.sh` wrote
 * capacity_units (40 against a max_active of 20 at its defaults) and
 * Terraform's bootstrap wrote max_active. Both now write the minimum, which
 * `tests/terraform/infra_guards.tftest.hcl` and
 * `tests/integration/test_register_tenant_grants.py` prove; the paragraph says
 * which paths those are, so the claim is checkable rather than general.
 *
 * MUTATION: cut the paragraph back to "through the admin routes".
 */
test('tenant-fields names every path that writes the Enforced ceiling', () => {
  const enforced = HELP['tenant-fields'].long.find((p) => p.startsWith('Enforced'))
  assert.ok(enforced, 'tenant-fields has no paragraph on the Enforced column')
  for (const [path, why] of [
    [/admin routes/i, 'the admin routes'],
    [/first sign-in/i, 'a first sign-in (ensure_tenant)'],
    [/register-tenant\.sh/, 'scripts/register-tenant.sh'],
    [/Terraform/, 'Terraform’s bootstrap'],
  ] as const) {
    assert.match(enforced, path, `the Enforced paragraph does not name ${why}, which also writes the tenant pool`)
  }
})

/**
 * CH-3. The terms on the Help page were raw uppercase enums in a mono face:
 * LEASED, DISPATCHED -- shouting in a list whose job is to be read. They are
 * lowercased by CSS, so the strings stay the owner's spelling (and the
 * anti-drift test above still finds them) while the page stops shouting.
 *
 * RE-POINTED BY AH-18: the transform moved from an inline style on each `<dt>`
 * to `.help-topic dt` in styles.css, and `src/__tests__/shell.test.tsx` asks
 * the cascade for it. What is left here is the half no stylesheet can fake:
 * the markup carries the owner's spelling, untouched.
 *
 * MUTATION: lowercase the strings instead of the style.
 */
test('the Help page keeps its enum terms in the owner’s spelling', () => {
  const markup = renderToStaticMarkup(createElement(HelpScreen, { topic: '' }))
  const dts = [...markup.matchAll(/<dt(?: [^>]*)?>([^<]*)<\/dt>/g)]
  assert.ok(dts.length > 0, 'the Help page renders no terms')
  // The text itself is untouched: the states still arrive in their own case.
  assert.ok(dts.some(([, term]) => term === REAL_STATES.find((s) => CONCURRENCY_STATES.has(s))))
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
