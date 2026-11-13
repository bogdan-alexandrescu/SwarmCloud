// MOCK-HOOK FILE
// Invented fixture data for the plans-pipeline mock-ups (docs/design/plans-pipeline.md §3):
// GET /v1/pipeline, plan_revisions, plan_thread, plan_review_usage and the review chat are not
// served yet, so their shapes are drawn from the design. Every workspace, issue, run, pull request
// and person here is invented.

export const MOCK_RUN = 'run_mock_rerun01'
export const MOCK_ISSUE = 'example-org/example-api#4127'
const NOW = Date.parse('2026-11-13T10:00:00Z')
export const mockNow = NOW
export const ago = (min: number) => new Date(NOW - min * 60_000).toISOString()
const hex = (seed: string) => {
  let h = 0
  for (const c of seed) h = (h * 31 + c.charCodeAt(0)) >>> 0
  let out = ''
  while (out.length < 64) {
    h = (h * 1103515245 + 12345) >>> 0
    out += h.toString(16).padStart(8, '0')
  }
  return out.slice(0, 64)
}
export const DIGEST = { 1: `sha256:${hex('rev-one')}`, 2: `sha256:${hex('rev-two')}`, 3: `sha256:${hex('rev-three')}` } as Record<number, string>
export const shortDigest = (d: string) => d.replace('sha256:', '').slice(0, 10)

export const PEOPLE: Record<string, string> = {
  'kit@example.com': 'Kit Arden',
  'lee@example.com': 'Lee Moss',
  'sam@example.com': 'Sam Ortiz',
  planner: 'Planner',
  'review-agent': 'Review agent',
}

// ---- the plan, revision 2 (the one on screen) -------------------------------------------------
const steps = (rev: number) => [
  {
    step_id: 's1',
    title: 'Keep a passed check when the rebase leaves its inputs unchanged',
    prompt:
      'In mergestep.py, key each check result by the tree hash of the paths the check reads (checkcache.py). After a rebase, re-run only the checks whose key changed. Commit the red test first.',
    files: ['apps/swarm-api/swarm_api/mergestep.py', 'apps/swarm-api/swarm_api/checkcache.py', 'tests/unit/api/test_mergestep_rerun.py'],
    estimate: '1 agent-hour',
    rationale:
      'The re-run rule lives in one function; keying by the paths a check reads rather than the commit keeps a docs-only rebase from re-running the 14 checks.',
  },
  {
    step_id: 's2',
    title: rev >= 2 ? 'Re-run any check older than 24 hours, and document the rule' : 'Document the re-run rule',
    prompt:
      rev >= 2
        ? 'Add CHECK_MAX_AGE_HOURS = 24 beside the cache: a result older than that is re-run whatever changed. Write the rule and its bound in docs/merge-step.md.'
        : 'Write the new re-run rule in docs/merge-step.md.',
    files: rev >= 2 ? ['apps/swarm-api/swarm_api/checkcache.py', 'docs/merge-step.md'] : ['docs/merge-step.md'],
    depends_on: ['s1'],
    estimate: rev >= 3 ? '1 agent-hour' : '0.5 agent-hour',
    rationale:
      rev >= 2
        ? 'Added in revision 2 for the comment on risk: a cached pass must not outlive a flaky dependency, so the age bound sits with the rule.'
        : 'The rule changes what a reviewer sees on the pull request, so it is written down where the merge step is documented.',
  },
]

export function mockPlan(rev = 2): Record<string, unknown> {
  const reqs = [
    'A rebase that leaves every path a check reads unchanged re-runs no check',
    'A rebase that changes a path re-runs every check that reads it',
    ...(rev >= 2 ? ['A check result older than 24 hours is re-run, whatever changed'] : []),
  ]
  return {
    summary: 'Re-run only the checks a rebase can change',
    purpose: 'Re-run only the checks a rebase can change',
    why: {
      line: 'The merge step re-runs all 14 checks after every rebase, so a docs-only rebase holds a pull request for 40 minutes.',
      evidence: ['example-org/example-api#4127', 'pull requests #4051, #4063 and #4088 each waited 38-44 min on an unchanged tree'],
    },
    rationale:
      'Smallest change that ends the wait: the merge step already knows which paths each check reads, so the cache needs no new registration. A per-commit cache was weighed and dropped, because every rebase makes a new commit.',
    requirements: reqs,
    changes: [
      { file: 'apps/swarm-api/swarm_api/mergestep.py', kind: 'modify', what: 're-run rule reads the cache' },
      { file: 'apps/swarm-api/swarm_api/checkcache.py', kind: 'add', what: 'results keyed by the tree hash of read paths' },
      { file: 'tests/unit/api/test_mergestep_rerun.py', kind: 'test', what: 'unchanged, changed and stale cases' },
      { file: 'docs/merge-step.md', kind: 'modify', what: 'the rule and its bound' },
    ],
    graph: {
      nodes: [
        { id: 'n1', file: 'apps/swarm-api/swarm_api/mergestep.py', label: 'mergestep.py', kind: 'changed' },
        { id: 'n2', file: 'apps/swarm-api/swarm_api/checkcache.py', label: 'checkcache.py', kind: 'changed' },
        { id: 'n3', file: 'tests/unit/api/test_mergestep_rerun.py', label: 'test_mergestep_rerun.py', kind: 'test' },
        { id: 'n4', file: 'docs/merge-step.md', label: 'merge-step.md', kind: 'changed' },
        { id: 'n5', file: 'apps/swarm-api/swarm_api/routes/runs.py', label: 'routes/runs.py', kind: 'caller' },
      ],
      edges: [
        { from: 'n5', to: 'n1', kind: 'calls' },
        { from: 'n1', to: 'n2', kind: 'calls' },
        { from: 'n3', to: 'n1', kind: 'tests' },
      ],
    },
    risk: {
      level: 'medium',
      factors: ['A wrong cache key would let a pull request merge on a check that never ran on its tree'],
      blast_radius: 'Every merge in the workspace goes through this rule',
      invariants: [],
      mitigations: ['The key covers every path the check declares it reads', ...(rev >= 2 ? ['A cached result expires after 24 hours'] : [])],
      rollback: rev >= 2 ? 'Set MERGE_CHECK_CACHE=off: every check re-runs, as today' : 'Revert the pull request',
    },
    validation: {
      tests: [
        { file: 'tests/unit/api/test_mergestep_rerun.py', proves: 'an unchanged tree re-runs nothing', requirements: ['R1'] },
        { file: 'tests/unit/api/test_mergestep_rerun.py', proves: 'a changed path re-runs its checks', requirements: ['R2'] },
        ...(rev >= 2 ? [{ file: 'tests/unit/api/test_mergestep_age.py', proves: 'a 25-hour-old pass is re-run', requirements: ['R3'] }] : []),
      ],
      qa: [
        { check: 'Rebase a docs-only pull request in the dev workspace; no check re-runs', where: 'dev', when: 'after deploy' },
        { check: 'Change one source file in a pull request; only its checks re-run', where: 'dev', when: 'after deploy' },
      ],
    },
    steps: steps(rev),
    estimate: rev >= 3 ? '2 agent-hours' : '1.5 agent-hours',
    overlaps: [
      {
        ref: 'example-org/example-api#4090',
        kind: 'pull_request',
        note: 'Merge queue retries: touches the same re-run function',
        action: 'informational',
        theirs: [{ file: 'apps/swarm-api/swarm_api/mergestep.py', what: 'retry count on a failed merge' }],
      },
    ],
  }
}

export const EXPECTED_COST = {
  low: 3.1,
  high: 6.4,
  basis: '2 steps × 1.5 agent-hours at opus list price, plus review and fix',
  prices_at: ago(30),
}

// ---- revisions (§3.3) --------------------------------------------------------------------------
export interface MockRevision {
  revision: number
  digest: string
  author: { kind: 'planner' | 'person'; by: string }
  made_by: 'plan' | 'revise' | 'edit' | 'add_diagram'
  requested_by?: string
  answers: string[]
  changed: { path: string; op: 'add' | 'remove' | 'replace'; was?: string; now?: string }[]
  cost: { usd: number | null; model_class: string; why_none?: string } | null
  at: string
}
export const REVISIONS: MockRevision[] = [
  { revision: 1, digest: DIGEST[1]!, author: { kind: 'planner', by: 'planner' }, made_by: 'plan', answers: [], changed: [], cost: { usd: 1.2, model_class: 'opus' }, at: ago(300) },
  {
    revision: 2,
    digest: DIGEST[2]!,
    author: { kind: 'planner', by: 'planner' },
    made_by: 'revise',
    requested_by: 'kit@example.com',
    answers: ['pth_a1', 'pth_a2'],
    changed: [
      { path: 'requirements.3', op: 'add', now: 'A check result older than 24 hours is re-run, whatever changed' },
      { path: 'steps.s2.title', op: 'replace', was: 'Document the re-run rule', now: 'Re-run any check older than 24 hours, and document the rule' },
      { path: 'steps.s2.files', op: 'add', now: 'apps/swarm-api/swarm_api/checkcache.py' },
      { path: 'risk.mitigations.2', op: 'add', now: 'A cached result expires after 24 hours' },
      { path: 'risk.rollback', op: 'replace', was: 'Revert the pull request', now: 'Set MERGE_CHECK_CACHE=off: every check re-runs, as today' },
      { path: 'validation.tests.3', op: 'add', now: 'a 25-hour-old pass is re-run' },
    ],
    cost: { usd: 1.65, model_class: 'opus' },
    at: ago(42),
  },
]
export const REVISION_3: MockRevision = {
  revision: 3,
  digest: DIGEST[3]!,
  author: { kind: 'person', by: 'lee@example.com' },
  made_by: 'edit',
  answers: [],
  changed: [
    { path: 'steps.s2.estimate', op: 'replace', was: '0.5 agent-hour', now: '1 agent-hour' },
    { path: 'estimate', op: 'replace', was: '1.5 agent-hours', now: '2 agent-hours' },
  ],
  cost: null,
  at: ago(1),
}

/** A changed path (§3.3 `changed[].path`) in words, for the margin and the superseded line. */
export function changeWord(path: string): string {
  const step = /^steps\.s(\d+)\.(\w+)$/.exec(path)
  if (step) return `step ${step[1]} ${step[2]}`
  const n = /\.(\d+)$/.exec(path)?.[1]
  if (path.startsWith('requirements.')) return `requirement R${n}`
  if (path.startsWith('risk.mitigations.')) return `mitigation ${n}`
  if (path === 'risk.rollback') return 'rollback'
  if (path.startsWith('validation.tests.')) return `red-first test T${n}`
  if (path.startsWith('diagrams.')) return `diagram ${n}`
  if (path === 'estimate') return 'plan estimate'
  return path
}

/**
 * The console's own fixtures name a real model provider in the sidebar's Global pool line; the
 * mock-up pages show an invented one instead, so no frame names a real company.
 */
export function mockScrub(text: string): string {
  if (typeof location === 'undefined') return text
  const on = location.pathname.startsWith('/automate/pipeline') || location.pathname.includes(MOCK_RUN) || location.search.includes('plans=')
  return on ? text.replace(/anthropic/g, 'provider-a') : text
}

// ---- the diagram pinned to the plan (§6.2) -------------------------------------------------------
export interface MockDiagram {
  diagram_id: string
  kind: 'flow' | 'sequence'
  title: string
  why: string
  explains: { part: 'plan' | 'step'; ref: string | null }
  in_revision: number
}
export const PINNED: MockDiagram[] = [
  {
    diagram_id: 'd-rerun-rule',
    kind: 'flow',
    title: 'Which checks a rebase re-runs',
    why: 'The rule has three outcomes; the flow shows where a cached pass is kept and where it is thrown away.',
    explains: { part: 'plan', ref: null },
    in_revision: 1,
  },
]
export const ASKED: MockDiagram = {
  diagram_id: 'd-cache-seq',
  kind: 'sequence',
  title: 'Merge step, check cache and CI after a rebase',
  why: 'Asked on step 1: who reads the cache, and when CI is called.',
  explains: { part: 'step', ref: 's1' },
  in_revision: 3,
}

// ---- the anchored thread (§3.4), with derived events --------------------------------------------
export interface MockEntry {
  id: string
  kind: 'comment' | 'answer' | 'diagram' | 'event' | 'opening' | 'asking'
  anchor: string // ab-head | ab-map | ab-pair | ab-steps:s1 | ab-steps:s2 | ab-over
  by: string
  text: string
  on_revision: number
  answered_by_revision?: number
  at: string
  cost?: number
  tone?: 'ok' | 'bad' | 'warn'
}
export const THREAD: MockEntry[] = [
  { id: 'ev_planned', kind: 'opening', anchor: 'ab-head', by: 'planner', text: 'Drew one diagram: Which checks a rebase re-runs.', on_revision: 1, at: ago(300) },
  { id: 'pth_a1', kind: 'comment', anchor: 'ab-pair', by: 'kit@example.com', text: 'A pass cached for days would hide a flaky dependency. Bound its age?', on_revision: 1, answered_by_revision: 2, at: ago(120) },
  { id: 'pth_a2', kind: 'comment', anchor: 'ab-steps:s2', by: 'kit@example.com', text: 'Put the age bound in this step rather than a third one.', on_revision: 1, answered_by_revision: 2, at: ago(118) },
  { id: 'pth_a3', kind: 'comment', anchor: 'ab-over', by: 'sam@example.com', text: '#4090 lands first; rebase on it before step 1.', on_revision: 2, at: ago(20) },
]
export const AFTER_APPROVAL: MockEntry[] = [
  { id: 'ev_approved', kind: 'event', anchor: 'ab-head', by: 'kit@example.com', text: 'Approved revision 2', on_revision: 2, at: ago(30), tone: 'ok' },
  { id: 'ev_s1', kind: 'event', anchor: 'ab-steps:s1', by: 'planner', text: 'task_7c1e0a · done in 52 min', on_revision: 2, at: ago(28) },
  { id: 'ev_s2', kind: 'event', anchor: 'ab-steps:s2', by: 'planner', text: 'task_9b42d1 · done in 24 min', on_revision: 2, at: ago(27) },
  { id: 'ev_pr', kind: 'event', anchor: 'ab-head', by: 'planner', text: 'Opened pull request #4133', on_revision: 2, at: ago(24) },
  { id: 'ev_ci1', kind: 'event', anchor: 'ab-head', by: 'planner', text: 'CI round 1 · 13 of 14 passed · unit-api failed', on_revision: 2, at: ago(20), tone: 'bad' },
  { id: 'ev_fix', kind: 'event', anchor: 'ab-head', by: 'planner', text: 'Fix round 1 · task_2f80c3', on_revision: 2, at: ago(14) },
  { id: 'ev_ci2', kind: 'event', anchor: 'ab-head', by: 'planner', text: 'CI round 2 · 14 of 14 passed', on_revision: 2, at: ago(8), tone: 'ok' },
]

// ---- the review chat (§7) ---------------------------------------------------------------------------
export const CHAT = {
  model: 'sonnet',
  next: 'sonnet · standard',
  usage: { review_usd: { spent: 2.85, cap: 25 }, chat_turns: { used: 3, cap: 40 }, diagram_asks: { used: 1, cap: 20 }, replans: { used: 1, cap: 5 } },
  turns: [
    { who: 'kit@example.com', text: 'Why the tree hash of the read paths and not the commit?', at: ago(16) },
    {
      who: 'review-agent',
      text: 'Every rebase makes a new commit, so a commit key would miss every time. The paths a check reads are already declared in checks.yaml; hashing those trees changes only when an input changes.',
      at: ago(15),
      cost: 0.62,
    },
    { who: 'kit@example.com', text: 'What if a check reads a path it does not declare?', at: ago(6) },
    {
      who: 'review-agent',
      text: 'Then its pass could be kept on a changed tree. Step 1 could fail closed: a check with no declared paths is always re-run. That is a plan change; Request changes with it, or edit.',
      at: ago(5),
      cost: 0.71,
      proposal: true,
    },
  ],
}

// ---- the board (§3.2) ----------------------------------------------------------------------------------
export type Stage = 'found' | 'planned' | 'running' | 'pr_ci' | 'ended'
export type SourceKind = 'sweep' | 'failure_review' | 'observer' | 'flake' | 'hand'
export interface MockCard {
  id: string
  stage: Stage
  source: SourceKind
  by?: string
  title: string
  issue: string | null
  state: string
  waits: boolean
  ask?: string
  expires_min?: number | null
  since_min: number
  last_word?: { by: string; text: string } | null
  revision?: number | null
  pr?: number | null
  checks?: string | null
  cost?: number | null
  cost_why?: string
  to?: string
}
export const CARDS: MockCard[] = [
  { id: 'apr_obs_01', stage: 'found', source: 'observer', title: 'Lease release logs a warning on every cancel', issue: null, state: 'proposed', waits: true, ask: 'File as an issue?', expires_min: 300, since_min: 190, last_word: null },
  { id: 'apr_flk_02', stage: 'found', source: 'flake', title: 'test_scheduler_window fails 1 run in 9 on main', issue: null, state: 'proposed', waits: true, ask: 'File as an issue?', expires_min: 1260, since_min: 70, last_word: null },
  { id: 'run:run_mock_pl03', stage: 'found', source: 'sweep', title: 'Export button ignores the date filter', issue: 'example-org/example-web#812', state: 'planning', waits: false, since_min: 6, cost: null, cost_why: 'planning' },
  { id: 'fr-91ac', stage: 'found', source: 'failure_review', title: 'Checkpoint upload times out over 2 GiB', issue: null, state: 'filed', waits: false, since_min: 1700 },
  { id: `run:${MOCK_RUN}`, stage: 'planned', source: 'sweep', title: 'Merge step re-runs every check after a rebase', issue: MOCK_ISSUE, state: 'waiting', waits: true, ask: 'Approve plan?', expires_min: 1380, since_min: 42, last_word: { by: 'Sam Ortiz', text: '#4090 lands first; rebase on it before step 1.' }, revision: 2, to: `automate/approvals/run:${MOCK_RUN}` },
  { id: 'run:run_mock_pl05', stage: 'planned', source: 'failure_review', title: 'Retry a 503 from the artifact store once', issue: 'example-org/example-api#4119', state: 'held', waits: true, ask: 'Approve held plan?', expires_min: 95, since_min: 1500, last_word: { by: 'Planner', text: 'Touches the workflows path: a second member approves.' }, revision: 1 },
  { id: 'run:run_mock_pl06', stage: 'planned', source: 'hand', by: 'Lee Moss', title: 'Rename the usage export columns', issue: 'example-org/example-web#806', state: 'planned', waits: false, since_min: 25, revision: 1, last_word: { by: 'Lee Moss', text: 'Hold until the finance sheet is updated.' } },
  { id: 'run:run_mock_rn07', stage: 'running', source: 'sweep', title: 'Paginate the workspace members list', issue: 'example-org/example-web#799', state: 'running', waits: false, since_min: 35, revision: 1, cost: 2.4 },
  { id: 'run:run_mock_rn08', stage: 'running', source: 'failure_review', title: 'Close the stream when a client disconnects', issue: 'example-org/example-api#4102', state: 'approved', waits: false, since_min: 4, revision: 3, cost: null, cost_why: 'no attempt yet', last_word: { by: 'auto-approval', text: 'Approved by the schedule’s gate' } },
  { id: 'apr_mrg_09', stage: 'pr_ci', source: 'sweep', title: 'Show the region on the capacity card', issue: 'example-org/example-web#790', state: 'waiting', waits: true, ask: 'Approve merge?', expires_min: 2600, since_min: 300, pr: 4128, checks: '14 of 14 passed' },
  { id: 'run:run_mock_pc10', stage: 'pr_ci', source: 'hand', by: 'Kit Arden', title: 'Drop the unused queue metrics', issue: 'example-org/example-api#4095', state: 'fixing', waits: false, since_min: 50, pr: 4126, checks: '12 of 14 passed', cost: 4.1 },
  { id: 'run:run_mock_pc11', stage: 'pr_ci', source: 'flake', title: 'Pin the clock in test_lease_expiry', issue: 'example-org/example-api#4110', state: 'checking', waits: false, since_min: 12, pr: 4131, checks: '9 of 14 · running', cost: 0.9 },
  { id: 'run:run_mock_en12', stage: 'ended', source: 'sweep', title: 'Trim the trailing slash from webhook URLs', issue: 'example-org/example-api#4081', state: 'merged', waits: false, since_min: 200, pr: 4117, cost: 3.6 },
  { id: 'run:run_mock_en13', stage: 'ended', source: 'failure_review', title: 'Raise the pod start timeout for large images', issue: 'example-org/example-api#4077', state: 'rejected', waits: false, since_min: 900, last_word: { by: 'Kit Arden', text: 'Rejected: the timeout is the platform’s, not this repo’s.' }, cost: 1.1 },
  { id: 'apr_obs_14', stage: 'ended', source: 'observer', title: 'Dashboard query scans the whole collection', issue: 'example-org/example-api#4099', state: 'filed', waits: false, since_min: 2000 },
  { id: 'run:run_mock_en15', stage: 'ended', source: 'flake', title: 'Seed the random order in test_capacity', issue: 'example-org/example-api#4060', state: 'merged', waits: false, since_min: 3100, pr: 4071, cost: 1.4 },
]
export const HIDDEN_ENDED = 9 // ended cards past the board's bound, counted by the server (by_stage.ended.hidden)
export const NEXT_FIRING = { name: 'Nightly issue sweep', in_min: 220 }
