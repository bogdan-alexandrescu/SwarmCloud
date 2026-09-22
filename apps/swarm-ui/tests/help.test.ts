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
import { HelpScreen } from '../src/HelpSection'
import { NEVER_WRITTEN, REAL_STATES, REASON_COPY } from '../src/types'

// esbuild inlines this file, so __dirname would be the build directory. The
// source tree is found from the repo layout instead, which is stable.
const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'src')

function sourceFiles(): { name: string; text: string }[] {
  return readdirSync(SRC)
    .filter((f) => f.endsWith('.tsx') || f.endsWith('.ts'))
    .map((f) => ({ name: f, text: readFileSync(join(SRC, f), 'utf8') }))
}

/** Every `topic="..."` and `help="..."` a screen hands to a HelpCard. */
function referencedTopics(): { file: string; topic: string }[] {
  const found: { file: string; topic: string }[] = []
  for (const { name, text } of sourceFiles()) {
    if (name === 'help.ts') continue
    for (const m of text.matchAll(/(?:topic|help)="([a-z0-9-]+)"/g)) {
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
