/**
 * EACH SECTION ANSWERS A QUESTION, AND IS WRITTEN AS ONE (AH-23).
 *
 * `SECTIONS` in App.tsx gives each of the three sections and the landing
 * screen the question it answers; the head's `?` prints it. Three were
 * questions and Admin's was an instruction -- "Change a ceiling, see who is
 * registered to use this platform, and count what it has done." -- which is a
 * list of things to do, not the thing a reader arrives wanting to know. It now
 * asks what each of its three tabs answers, in tab order.
 *
 * AND ITS ONE LIVE COPY AGREES WITH IT. docs/web-ui/redesign.md §2 is the
 * architecture table the sections were designed from, and it restates each
 * question. A restatement nothing checks is how this repository keeps shipping
 * a value stated twice and true once, so the table is read here and held to
 * the array, verbatim. (redesign-v2.md §2.4 and ui-audit §B6.11 are dated
 * briefs, kept as written by the owner's D6 answer of 2026-09-24, and are not
 * held to it.)
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

// App.tsx's module body touches no DOM at import, but `route.test.ts` installs
// the one property its router reads, and so does this, before the import.
;(globalThis as { window?: unknown }).window = { location: { hash: '' } }
const { SECTIONS } = await import('../src/App')

// esbuild bundles this file into apps/swarm-ui/.test-build, so the repository
// root is three levels up from there.
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..')

test('every section question is a question (AH-23)', () => {
  assert.ok(SECTIONS.length >= 4, `only ${SECTIONS.length} sections were read`)
  for (const s of SECTIONS) {
    assert.match(s.question.trim(), /\?$/, `${s.label}: "${s.question}" is not a question`)
  }
})

test('Admin asks what each of its tabs answers, in tab order (AH-23)', () => {
  const admin = SECTIONS.find((s) => s.id === 'admin')
  assert.ok(admin, 'there is no Admin section')
  assert.equal(
    admin.question,
    'What is each ceiling set to, who is registered to use this platform, and how many tasks are in each state?',
  )
})

test('redesign.md §2 carries every section question verbatim', () => {
  const doc = readFileSync(resolve(ROOT, 'docs', 'web-ui', 'redesign.md'), 'utf8')
  const rows = doc.split('\n').filter((l) => l.startsWith('| **'))
  assert.ok(rows.length >= SECTIONS.length, `the architecture table was not found in redesign.md`)
  for (const s of SECTIONS) {
    const row = rows.find((l) => l.startsWith(`| **${s.label}** |`))
    assert.ok(row, `redesign.md §2 has no row for ${s.label}`)
    assert.ok(row.includes(`| ${s.question} |`), `redesign.md §2's question for ${s.label} is not App.tsx's:\n${row}`)
  }
})
