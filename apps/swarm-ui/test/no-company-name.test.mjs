// The console names no company, no person and no real deployment: no file under
// apps/swarm-ui matches an entry of the deployment pattern list.
//
// WHY. Owner decision, 2026-10-19 (#1647), widened on 7 November 2026 to the
// generic repository (docs/design/generic-repository.md). The console used to
// say "Internal Laya, which stays inside <the company>" and its fixtures were
// full of the live deployment's addresses, people, repositories and service
// accounts. Those became "your network" and the placeholder set
// (scripts/guards/placeholders.json). A copy edit or a fixture pasted from a
// live response puts a value straight back, and nothing on screen would flag
// it, so this walks every file rather than rendering pages.
//
// WHAT IS LIVE comes from the pattern list (test/deploymentPatterns.ts), never
// from this file: a guard that spells the value is itself a finding. With no
// list the walk is skipped and says why. A hit names the path, the line and the
// entry's id, never the value.
//
// SCOPE. All of apps/swarm-ui -- src/ (copy, fixtures, comments, CSS, tests),
// test/, tests/, index.html, public/ if it exists, and the top-level files --
// except node_modules/, dist/, coverage/ and dot-directories, which hold what
// the package installs or builds rather than what it says.
//
// MUTATION. A planted list, built at run time from a placeholder token, turns
// the guard red on a file that carries the planted value (the last test).

import test from 'node:test'
import assert from 'node:assert/strict'
import { randomBytes } from 'node:crypto'
import { mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

import { LISTED, NO_LIST, listedIn, parseList, plantedList } from './deploymentPatterns.ts'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const SKIP_DIRS = new Set(['node_modules', 'dist', 'coverage'])
const PLACEHOLDERS = JSON.parse(readFileSync(join(root, '../../scripts/guards/placeholders.json'), 'utf8'))

function files(dir) {
  const out = []
  for (const name of readdirSync(dir)) {
    if (name.startsWith('.') || SKIP_DIRS.has(name)) continue
    const p = join(dir, name)
    if (statSync(p).isDirectory()) out.push(...files(p))
    else out.push(p)
  }
  return out
}

/** Every `path:line list:<n>` whose text matches an entry. */
function mentions(paths, entries) {
  const hits = []
  for (const p of paths) {
    const rel = relative(root, p).split(sep).join('/')
    readFileSync(p, 'utf8').split('\n').forEach((line, i) => {
      for (const id of listedIn(line, entries)) hits.push(`${rel}:${i + 1} ${id}`)
    })
  }
  return hits
}

const all = files(root)

test('the walk reaches the console source, its tests and index.html', () => {
  // A walk that visits nothing passes hardest when it is most broken.
  const rel = all.map((p) => relative(root, p).split(sep).join('/'))
  assert.ok(rel.includes('index.html'), 'index.html was not visited')
  assert.ok(rel.includes('src/App.tsx'), 'src/App.tsx was not visited')
  assert.ok(rel.includes('src/api.ts'), 'src/api.ts was not visited')
  assert.ok(rel.some((p) => p.startsWith('src/__tests__/')), 'src/__tests__/ was not visited')
  assert.ok(rel.some((p) => p.startsWith('test/')), 'test/ was not visited')
  assert.ok(rel.some((p) => p.startsWith('src/styles/')), 'src/styles/ was not visited')
  assert.ok(!rel.some((p) => p.startsWith('node_modules/')), 'node_modules/ was walked')
})

test('no file under apps/swarm-ui names a listed value', { skip: LISTED === null ? NO_LIST : false }, () => {
  const hits = mentions(all, LISTED)
  assert.deepEqual(hits, [], `${hits.length} line(s) name a listed value:\n${hits.join('\n')}`)
})

test('the guard names the file, line and entry of a planted listed value', () => {
  // The planted value is a placeholder token made unique at run time, so this
  // file names nothing and the planted list matches nothing else in the tree.
  const word = `${PLACEHOLDERS.github.owner}-${randomBytes(4).toString('hex')}`
  const entries = parseList(plantedList([word]))
  const dir = mkdtempSync(join(tmpdir(), 'swarm-ui-guard-'))
  try {
    const planted = join(dir, 'Planted.tsx')
    writeFileSync(planted, `// clean\nconst repo = '${word.toUpperCase().replaceAll('-', '_')}/widgets'\n`)
    const hits = mentions([planted], entries)
    assert.equal(hits.length, 1, hits.join('\n'))
    assert.ok(hits[0].endsWith('Planted.tsx:2 list:2'), hits[0])
    assert.ok(!hits[0].includes(word), 'a hit must not print the value')
    assert.deepEqual(mentions(all, entries), [], 'the planted value is in the tree')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

test('a list with a wrong digest is refused, not read as empty', () => {
  const text = plantedList([`${PLACEHOLDERS.github.owner}-${randomBytes(4).toString('hex')}`])
  assert.throws(() => parseList(text.replace(/^# sha256 [0-9a-f]{8}/, '# sha256 00000000')), /digest mismatch/)
  assert.throws(() => parseList(text.split('\n').slice(1).join('\n')), /no '# sha256/)
})
