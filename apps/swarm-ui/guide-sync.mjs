// Copies the user guide's Markdown (docs/user-guide/*.md, the one source) into
// src/guidePages.ts, which the console bundles and draws at /guide.
//
// WHY A COPY RATHER THAN AN IMPORT: the image build (images/swarm-ui/Dockerfile)
// copies only apps/swarm-ui/ into its build stage, so a Vite import reaching
// ../../docs would build here and fail in the image. The copy is generated,
// never edited:
//
//     node apps/swarm-ui/guide-sync.mjs           writes it
//     node apps/swarm-ui/guide-sync.mjs --check   exits 1 when it is stale
//
// The --check is a CI command (application.yml's quick-checks), not a test: a
// test that opened docs/ would turn the console's unit tests red on a docs
// edit (issue 71; docs/ci.md "A test reads only its own tree").
import { existsSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const args = process.argv.slice(2)
const check = args.includes('--check')
if (args.some((a) => a !== '--check')) {
  console.error('usage: node apps/swarm-ui/guide-sync.mjs [--check]')
  process.exit(2)
}

const here = dirname(fileURLToPath(import.meta.url))
const source = join(here, '..', '..', 'docs', 'user-guide')
const target = join(here, 'src', 'guidePages.ts')

const names = readdirSync(source)
  .filter((n) => n.endsWith('.md'))
  .sort()
const lines = [
  '// GENERATED from docs/user-guide/*.md by `node apps/swarm-ui/guide-sync.mjs`.',
  '// Edit the Markdown, then run that command; `node apps/swarm-ui/guide-sync.mjs',
  '// --check` fails in CI while this file and the Markdown differ.',
  '',
  'export const GUIDE_SOURCES: Readonly<Record<string, string>> = {',
  ...names.map((n) => `  ${JSON.stringify(n.slice(0, -3))}: ${JSON.stringify(readFileSync(join(source, n), 'utf8'))},`),
  '}',
  '',
]
const text = lines.join('\n')
if (check) {
  const current = existsSync(target) ? readFileSync(target, 'utf8') : null
  if (current !== text) {
    console.error(`${target} is stale against docs/user-guide/*.md: run node apps/swarm-ui/guide-sync.mjs`)
    process.exit(1)
  }
  console.log(`${target} is current (${names.length} pages)`)
} else {
  writeFileSync(target, text)
  console.log(`wrote ${names.length} pages to ${target}`)
}
