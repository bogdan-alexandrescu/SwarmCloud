import { readFileSync, writeFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
const ROOT = 'apps/swarm-ui'
const SKIP = new Set(['node_modules', 'dist', 'coverage'])
const R = [
  [/swarm-agent-worker-u-bogdan@saga-agents-staging\.iam/g, 'swarm-agent-worker-u-bogdan@example-project.iam'],
  [/swarm-agent-worker-u-bogdan@saga-/g, 'swarm-agent-worker-u-bogdan@example-'],
  [/saga-agents-staging/g, 'example-project'],
  [/saga-agents\.iam/g, 'example-project.iam'],
  [/saga-swarm\.iam/g, 'example-project.iam'],
  [/bogdan@sagadigital\.example\.com/g, 'alice@digitalgroup.example.com'],
  [/bogdan\.a@saga\.xyz/g, 'ann.a@example.com'],
  [/bogdan@saga\.xyz/g, 'alice@example.com'],
  [/bogdan@saga\./g, 'alice@example.'],
  [/`bogdan@sa…`/g, '`alice@ex…`'],
  [/Ana@Saga\.xyz/g, 'Ana@Example.com'],
  [/SWARM\.SAGA\.XYZ/g, 'SWARM.EXAMPLE.COM'],
  [/swarm\.saga\.xyz/g, 'swarm.example.com'],
  [/@saga\. xyz/g, '@example. com'],
  [/…@saga\.x…/g, '…@example.c…'],
  [/%40saga\.xyz/g, '%40example.com'],
  [/saga\.xyz/g, 'example.com'],
  [/@saga\.(?=[`\s"'])/g, '@example.'],
  [/sagaxyz\/ai-studio/g, 'example-org/ai-studio'],
  [/owner: 'sagaxyz'/g, "owner: 'example-org'"],
  [/saga-xyz\//g, 'example-org/'],
  [/swarmcloud-saga/g, 'swarmcloud-example'],
  [/saga-personal/g, 'alice-personal'],
  [/ \(#1647, SAGA-COPY\)/g, ' (#1647)'],
]
let n = 0
function walk(d) {
  for (const e of readdirSync(d)) {
    if (SKIP.has(e) || e.startsWith('.')) continue
    const p = join(d, e)
    if (statSync(p).isDirectory()) { walk(p); continue }
    const s = readFileSync(p, 'utf8')
    if (!/saga/i.test(s)) continue
    let t = s
    for (const [a, b] of R) t = t.replace(a, b)
    if (t !== s) { writeFileSync(p, t); n++ }
  }
}
walk(ROOT)
console.log('files changed', n)
