// MOCK-HOOK FILE
/**
 * THE BAKERY SESSION'S WORK, for the Voice it tag-along mock-ups (tagAlong.ts):
 * while `?voicetagalong=bubble` is on for the tab, the fixture build draws the
 * session's own project on the pages the bubble takes the person to (Agents,
 * Workflows) and on Talk's Stage. Invented data only; types only are imported,
 * so api.ts can import this file without a cycle.
 */

import type { VoiceVisualDetail } from '../api'
import type { Task, Workflow } from '../types'

const KEY = 'swarm.voicetagalong'

/** The mock-up is on for this tab (the same switch tagAlong.ts reads). */
export function tagAlongOn(): boolean {
  if (typeof window === 'undefined') return false
  try {
    if (new URLSearchParams(window.location.search).get('voicetagalong') === 'bubble') return true
    return window.sessionStorage.getItem(KEY) === 'bubble'
  } catch {
    return false
  }
}

const REPO = 'https://github.com/example-org/corner-loaf-site'

/** The two running workflows building the site, and the workspace's other two. */
const WORKFLOW_TITLES: Readonly<Record<string, string>> = {
  wf_audit_01: 'Site build',
  wf_order_form_01: 'Order form',
  wf_5e5ad3b6f7da4299a839: 'Supplier price list import',
  wf_bcdc9180e4fb4a209f31: 'Allergen labels draft',
  wf_quiet_q_01: 'Menu photos',
}

/** The workspace's loose agents, by the order they are listed in. */
const AGENT_TITLES = [
  'Site build: menu page',
  'Order form: pickup slots',
  'Site build: homepage hero',
  'Order form: card payments',
  'Site build: opening hours page',
  'Order form: confirmation email',
  'Site build: contact page',
  'Check the site on a phone',
  'Compress the menu photos',
  'Write the allergen notes',
]

const ORDER = '_of'

/** The base fixture's workflow names (api.ts FIXTURE_WORKFLOW_TITLES), as the bakery's. */
const RENAMED: Readonly<Record<string, string>> = {
  'Audit the operator scripts and the Terraform': 'Site build',
  'Check the five contract invariants, then write one synthesis': 'Supplier price list import',
  'Research, draft and review the restore runbook': 'Allergen labels draft',
  Views: 'Menu photos',
}

/** The fixture's tasks, retitled as the bakery's; the Site build's steps cloned as the Order form's. */
export function bakeryTasks(rows: Task[]): Task[] {
  let loose = 0
  const titled = rows.map((t): Task => {
    const meta = { ...(t.metadata ?? {}) }
    const named = ['unit', 'title'].map((k) => meta[k]).find((v): v is string => typeof v === 'string' && v in RENAMED)
    const wf = t.workflow_id === null ? undefined : WORKFLOW_TITLES[t.workflow_id]
    const title = named !== undefined ? RENAMED[named] : wf
    if (title !== undefined) {
      for (const k of ['unit', 'title']) if (typeof meta[k] === 'string') meta[k] = title
      if (typeof meta.title !== 'string') meta.title = title
    } else {
      delete meta.unit
      meta.title = AGENT_TITLES[loose++ % AGENT_TITLES.length]
    }
    return { ...t, repository_url: REPO, metadata: meta }
  })
  const site = new Set(titled.filter((t) => t.metadata?.title === 'Site build' || t.metadata?.unit === 'Site build').map((t) => t.id))
  const order = titled
    .filter((t) => site.has(t.id))
    .map((t): Task => {
      const meta = { ...(t.metadata ?? {}) }
      for (const k of ['unit', 'title']) if (typeof meta[k] === 'string') meta[k] = WORKFLOW_TITLES.wf_order_form_01
      return { ...t, id: t.id + ORDER, workflow_id: t.workflow_id === null ? null : 'wf_order_form_01', metadata: meta }
    })
  return [...titled, ...order]
}

/** The fixture's workflows with the Order form beside the Site build, its steps on the cloned tasks. */
export function bakeryWorkflows(rows: Workflow[]): Workflow[] {
  const site = rows.find((w) => w.workflow_id === 'wf_audit_01')
  if (site === undefined) return rows
  const order: Workflow = {
    ...site,
    workflow_id: 'wf_order_form_01',
    steps: site.steps.map((s) => ({ ...s, task_id: s.task_id == null ? s.task_id : s.task_id + ORDER })),
  }
  const i = rows.indexOf(site)
  // The bakery's repository wherever a workflow names the fixture's (plain JSON documents, so a round trip is exact).
  return [...rows.slice(0, i + 1), order, ...rows.slice(i + 1)].map((w) => {
    let doc = JSON.stringify(w).replaceAll('agent-swarm-infra', 'corner-loaf-site')
    for (const [was, now] of Object.entries(RENAMED)) if (was !== 'Views') doc = doc.replaceAll(was, now)
    if (w.workflow_id === 'wf_order_form_01') doc = doc.replaceAll('Site build', 'Order form')
    return JSON.parse(doc) as Workflow
  })
}

const AT = '2026-10-19T11:58:00Z'

const HOMEPAGE = `<!doctype html><html><head><style>
body{margin:0;font:15px/1.5 Georgia,serif;background:#fbf4ea;color:#3b2a1e}
header{display:flex;justify-content:space-between;align-items:center;padding:14px 22px;background:#8a4b2a;color:#fff7ec}
header b{font-size:19px}nav span{margin-left:16px;font:13px/1 sans-serif}
.hero{padding:34px 22px;background:#f1d9b8}.hero h1{margin:0 0 8px;font-size:28px}.hero p{margin:0 0 16px}
.btn{display:inline-block;padding:9px 16px;border-radius:6px;background:#8a4b2a;color:#fff7ec;font:600 13px/1 sans-serif}
.row{display:flex;gap:12px;padding:18px 22px}.row div{flex:1;padding:12px;border-radius:8px;background:#fff;border:1px solid #e8d3b6}
.row h3{margin:0 0 4px;font-size:15px}.row p{margin:0;font-size:13px;color:#6b5240}
</style></head><body>
<header><b>Corner Loaf</b><nav><span>Menu</span><span>Order</span><span>Visit</span></nav></header>
<section class="hero"><h1>Bread baked at dawn</h1><p>Sourdough, pastries and cakes, two streets from the market.</p><span class="btn">Order for pickup</span></section>
<section class="row"><div><h3>Bread</h3><p>Country loaf, rye, seeded</p></div><div><h3>Pastries</h3><p>Croissant, cardamom bun</p></div><div><h3>Cakes</h3><p>To order, two days ahead</p></div></section>
</body></html>`

/** Talk's Stage for the bakery session: the homepage mock-up waiting for review. */
export const BAKERY_VISUALS: readonly VoiceVisualDetail[] = [
  {
    visual: {
      visual_id: 'vv_bakery_home',
      kind: 'mockup',
      title: 'Homepage, warm palette',
      version: 1,
      source: null,
      parts: [{ part_id: 'home-warm', title: 'Homepage, warm palette' }],
      lines: null,
      created_by: 'planner',
      updated_at: AT,
      approved: null,
    },
    body: JSON.stringify({ frames: [{ id: 'home-warm', label: 'Homepage, warm palette', html: HOMEPAGE }] }),
    notes: [],
  },
]
