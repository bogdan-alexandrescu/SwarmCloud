// The console names no company: no file under apps/swarm-ui matches COMPANY_NAME
// (test/companyName.ts), except that one file.
//
// WHY. Owner decision, 2026-10-19 (#1647). The console used to say "Internal
// Laya, which stays inside <the company>" and its fixtures were full of the
// company's addresses, repositories and service accounts. Those became "your
// network" and the RFC 2606 example.com family (alice@example.com,
// eng@example.com, example-org/widgets, swarm.example.com,
// ...@example-project.iam.gserviceaccount.com). A copy edit or a fixture
// pasted from a live response puts the name straight back, and nothing on
// screen would flag it, so this walks every file rather than rendering pages.
//
// SCOPE. All of apps/swarm-ui -- src/ (copy, fixtures, comments, CSS, tests),
// test/, tests/, index.html, public/ if it exists, and the top-level files --
// except node_modules/, dist/, coverage/ and dot-directories, which hold what
// the package installs or builds rather than what it says. The one exempt file
// is test/companyName.ts, named in COMPANY_NAME_FILE.
//
// MUTATION. Add a comment naming the company to any file under src/ and this
// fails, naming that file and line.

import test from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

import { COMPANY_NAME, COMPANY_NAME_FILE } from './companyName.ts'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const SKIP_DIRS = new Set(['node_modules', 'dist', 'coverage'])

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

function mentions(paths, exempt = COMPANY_NAME_FILE) {
  const hits = []
  for (const p of paths) {
    const rel = relative(root, p).split(sep).join('/')
    if (rel === exempt) continue
    readFileSync(p, 'utf8').split('\n').forEach((line, i) => {
      if (COMPANY_NAME.test(line)) hits.push(`${rel}:${i + 1}: ${line.trim().slice(0, 160)}`)
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
  assert.ok(rel.includes(COMPANY_NAME_FILE), `${COMPANY_NAME_FILE} was not visited, so its exemption is stale`)
  assert.ok(!rel.some((p) => p.startsWith('node_modules/')), 'node_modules/ was walked')
})

test('no file under apps/swarm-ui names the company', () => {
  const hits = mentions(all)
  assert.deepEqual(hits, [], `${hits.length} line(s) name the company:\n${hits.join('\n')}`)
})

test('the guard names the file and line of a mention', () => {
  // The exempt file is the one file known to hold the pattern: walked with no
  // exemption, the guard must point at its file and line.
  const hits = mentions([join(root, COMPANY_NAME_FILE)], null)
  assert.equal(hits.length, 1, hits.join('\n'))
  const at = readFileSync(join(root, COMPANY_NAME_FILE), 'utf8').split('\n').findIndex((l) => l.startsWith('export const COMPANY_NAME =')) + 1
  assert.ok(hits[0].startsWith(`${COMPANY_NAME_FILE}:${at}: export const COMPANY_NAME =`), hits[0])
})
