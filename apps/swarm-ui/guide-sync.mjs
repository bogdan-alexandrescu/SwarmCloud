// Copies the user guide's Markdown (docs/user-guide/*.md, the one source) into
// src/guidePages.ts, which the console bundles and draws at /guide.
//
// WHY A COPY RATHER THAN AN IMPORT: the image build (images/swarm-ui/Dockerfile)
// copies only apps/swarm-ui/ into its build stage, so a Vite import reaching
// ../../docs would build here and fail in the image. The copy is generated,
// never edited, and src/__tests__/guide.test.tsx fails when it differs from
// the Markdown, naming this command:
//
//     node apps/swarm-ui/guide-sync.mjs
import { readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const source = join(here, '..', '..', 'docs', 'user-guide')
const target = join(here, 'src', 'guidePages.ts')

const names = readdirSync(source)
  .filter((n) => n.endsWith('.md'))
  .sort()
const lines = [
  '// GENERATED from docs/user-guide/*.md by `node apps/swarm-ui/guide-sync.mjs`.',
  '// Edit the Markdown, then run that command; src/__tests__/guide.test.tsx',
  '// fails while this file and the Markdown differ.',
  '',
  'export const GUIDE_SOURCES: Readonly<Record<string, string>> = {',
  ...names.map((n) => `  ${JSON.stringify(n.slice(0, -3))}: ${JSON.stringify(readFileSync(join(source, n), 'utf8'))},`),
  '}',
  '',
]
writeFileSync(target, lines.join('\n'))
console.log(`wrote ${names.length} pages to ${target}`)
