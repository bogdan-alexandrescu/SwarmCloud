// MOCK-HOOK FILE
// Invented fixtures for the LLM Providers mock-up (docs/design/llm-providers.md §4.6, §6.2, §6.3).
// Every workspace, label and person here is made up.

export type Vendor = 'anthropic' | 'openai'
export type Kind = 'plan' | 'key'
export type LpState = 'available' | 'limited' | 'exhausted' | 'paused' | 'draining'

export interface Lend {
  tenant: string
  agents: number
  maxAgents: number | null // null: carried over, cap not set
  spend?: number // key: this month's estimate, USD
  spendCap?: number
}

export interface Win {
  usedPct: number
  resets: string
}

export interface LpAccount {
  id: string
  kind: Kind
  vendor: Vendor
  name: string
  label: string
  plan?: string // 'Max 20x', 'Pro' ...
  state: LpState
  until?: string // limited until / exhausted since
  drainLeft?: number
  holders: number
  maxAgents: number
  perWorkspace: { tenant: string; max: number }[]
  classes: string[]
  voiceIt: boolean
  today: number // attempts today on this account
  fallbackToday?: number // key: attempts that came here because the plans were spent
  fiveHour?: Win
  sevenDay?: Win
  spend?: number
  spendCap?: number | null
  addedBy: string
  addedAt: string
  replaced?: { by: string; at: string }
  probe?: { at: string; result: string; next: string }
  lends: Lend[]
}

export const VENDOR_LABEL: Record<Vendor, string> = { anthropic: 'Anthropic', openai: 'OpenAI' }
export const PLAN_LABEL: Record<Vendor, string> = { anthropic: 'Claude', openai: 'ChatGPT' }
export const CLASSES: Record<Vendor, string[]> = {
  anthropic: ['fable', 'opus', 'sonnet', 'haiku'],
  openai: ['gpt-large', 'gpt-mini', 'live'],
}
export const WORKSPACES = ['atlas-lab', 'harbor-ops', 'north-desk', 'quill-studio', 'ember-qa', 'tidewater', 'lantern-docs']

export const PLANS: LpAccount[] = [
  {
    id: 'p1', kind: 'plan', vendor: 'anthropic', name: 'Team primary', label: 'team-primary', plan: 'Max 20x',
    state: 'available', holders: 4, maxAgents: 6, perWorkspace: [{ tenant: 'lantern-docs', max: 2 }],
    classes: ['fable', 'opus', 'sonnet', 'haiku'], voiceIt: true, today: 61,
    fiveHour: { usedPct: 41, resets: '14:22' }, sevenDay: { usedPct: 63, resets: 'Nov 12' },
    addedBy: 'Rowan Vale', addedAt: 'Oct 2',
    lends: [
      { tenant: 'atlas-lab', agents: 1, maxAgents: 2 },
      { tenant: 'harbor-ops', agents: 2, maxAgents: 2 },
    ],
  },
  {
    id: 'p2', kind: 'plan', vendor: 'anthropic', name: 'Night shift', label: 'night-shift', plan: 'Max 5x',
    state: 'limited', until: '13:40', holders: 0, maxAgents: 3, perWorkspace: [],
    classes: ['sonnet', 'haiku'], voiceIt: false, today: 22,
    fiveHour: { usedPct: 100, resets: '13:40' }, sevenDay: { usedPct: 48, resets: 'Nov 14' },
    addedBy: 'Ines Okafor', addedAt: 'Oct 18',
    lends: [{ tenant: 'harbor-ops', agents: 0, maxAgents: null }],
  },
  {
    id: 'p3', kind: 'plan', vendor: 'openai', name: 'Codex plan', label: 'codex-plan', plan: 'Pro',
    state: 'available', holders: 2, maxAgents: 5, perWorkspace: [],
    classes: ['gpt-large', 'gpt-mini'], voiceIt: true, today: 34,
    fiveHour: { usedPct: 22, resets: '15:09' }, sevenDay: { usedPct: 35, resets: 'Nov 15' },
    addedBy: 'Rowan Vale', addedAt: 'Sep 29',
    lends: [{ tenant: 'atlas-lab', agents: 1, maxAgents: 2 }],
  },
  {
    id: 'p4', kind: 'plan', vendor: 'openai', name: 'Review bench', label: 'review-bench', plan: 'Plus',
    state: 'paused', holders: 1, maxAgents: 2, perWorkspace: [],
    classes: ['gpt-mini'], voiceIt: false, today: 3,
    fiveHour: { usedPct: 9, resets: '16:30' }, sevenDay: { usedPct: 71, resets: 'Nov 11' },
    addedBy: 'Theo Marsh', addedAt: 'Nov 4',
    lends: [],
  },
]

export const KEYS: LpAccount[] = [
  {
    id: 'k1', kind: 'key', vendor: 'anthropic', name: 'Build pool', label: 'build-pool',
    state: 'available', holders: 3, maxAgents: 8, perWorkspace: [{ tenant: 'tidewater', max: 3 }],
    classes: ['opus', 'sonnet', 'haiku'], voiceIt: false, today: 19, fallbackToday: 14,
    spend: 212.4, spendCap: 500, addedBy: 'Rowan Vale', addedAt: 'Nov 9',
    lends: [
      { tenant: 'atlas-lab', agents: 1, maxAgents: 2, spend: 38.1, spendCap: 100 },
      { tenant: 'harbor-ops', agents: 2, maxAgents: 3, spend: 96.5, spendCap: 100 },
    ],
  },
  {
    id: 'k2', kind: 'key', vendor: 'anthropic', name: 'Browser runs', label: 'browser-runs',
    state: 'limited', until: '12:14', holders: 2, maxAgents: 4, perWorkspace: [],
    classes: ['sonnet'], voiceIt: false, today: 27, fallbackToday: 0,
    spend: 88.0, spendCap: 150, addedBy: 'Ines Okafor', addedAt: 'Nov 9',
    lends: [],
  },
  {
    id: 'k3', kind: 'key', vendor: 'anthropic', name: 'Overflow', label: 'overflow',
    state: 'exhausted', until: '09:12', holders: 0, maxAgents: 4, perWorkspace: [],
    classes: ['fable', 'opus', 'sonnet', 'haiku'], voiceIt: false, today: 6, fallbackToday: 6,
    spend: 61.7, spendCap: null, addedBy: 'Theo Marsh', addedAt: 'Nov 9',
    probe: { at: '11:00', result: 'credit balance too low', next: '12:00' },
    lends: [{ tenant: 'atlas-lab', agents: 0, maxAgents: 1, spend: 12.0, spendCap: 40 }],
  },
  {
    id: 'k4', kind: 'key', vendor: 'openai', name: 'Shared voice', label: 'shared-voice',
    state: 'draining', drainLeft: 1, holders: 1, maxAgents: 3, perWorkspace: [],
    classes: ['gpt-large', 'gpt-mini', 'live'], voiceIt: true, today: 9, fallbackToday: 5,
    spend: 131.9, spendCap: 120, addedBy: 'Rowan Vale', addedAt: 'Nov 9', replaced: { by: 'Ines Okafor', at: 'Nov 10' },
    lends: [],
  },
  {
    id: 'k5', kind: 'key', vendor: 'openai', name: 'Codex metered', label: 'codex-metered',
    state: 'paused', holders: 0, maxAgents: 2, perWorkspace: [],
    classes: ['gpt-mini'], voiceIt: false, today: 0, fallbackToday: 0,
    spend: 4.2, spendCap: 50, addedBy: 'Theo Marsh', addedAt: 'Nov 10',
    lends: [{ tenant: 'harbor-ops', agents: 0, maxAgents: 1, spend: 0, spendCap: 25 }],
  },
]

const LONG = [
  'Platform evaluation harness, long-running regression sweeps',
  'Customer-facing documentation rewrite, second pass',
  'Weekend batch for the migration rehearsal',
]
const STATES: LpState[] = ['available', 'available', 'limited', 'available', 'paused', 'draining', 'available', 'exhausted']

function many(kind: Kind, n: number): LpAccount[] {
  const base = kind === 'plan' ? PLANS : KEYS
  const out: LpAccount[] = []
  for (let i = 0; i < n; i++) {
    const b = base[i % base.length]!
    let st: LpState = kind === 'plan' && STATES[i % STATES.length] === 'exhausted' ? 'available' : STATES[i % STATES.length]!
    // A plan with no 5-hour window left is Limited until it resets; one with room left is not.
    if (kind === 'plan' && b.fiveHour && b.fiveHour.usedPct >= 100 && st === 'available') st = 'limited'
    const fiveHour = kind === 'plan' && b.fiveHour ? (st === 'limited' ? { usedPct: 100, resets: '13:05' } : b.fiveHour.usedPct >= 100 ? { usedPct: 64, resets: '15:40' } : b.fiveHour) : undefined
    const today = (b.today * (i + 3)) % 47
    const name = i < 3 ? LONG[i]! : `${b.name} ${Math.floor(i / base.length) + 1}`
    out.push({
      ...b,
      id: `${b.id}-${i}`,
      name,
      label: i < 3 ? name.toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 40).replace(/-$/, '') : `${b.label}-${i + 1}`,
      state: st,
      until: st === 'limited' ? '13:05' : st === 'exhausted' ? '08:40' : undefined,
      drainLeft: st === 'draining' ? 2 : undefined,
      probe: st === 'exhausted' ? { at: '11:00', result: 'credit balance too low', next: '12:00' } : undefined,
      today,
      fiveHour,
      // what fell back here because the plans were spent is part of today's total, never more
      fallbackToday: b.fallbackToday === undefined ? undefined : Math.min(b.fallbackToday, today),
      lends: b.lends.slice(0, i % 3),
    })
  }
  if (kind === 'key') {
    out[1] = {
      ...out[1]!,
      lends: WORKSPACES.slice(0, 5).map((t, j) => ({ tenant: t, agents: j % 3, maxAgents: 2 + (j % 2), spend: 20 + j * 17.5, spendCap: j === 3 ? 80 : 100 })),
    }
  }
  return out
}

export function fixtureSet(fx: string | null): { plans: LpAccount[]; keys: LpAccount[] } {
  if (fx === 'many') return { plans: many('plan', 9), keys: many('key', 14) }
  if (fx === 'empty-keys') return { plans: PLANS, keys: [] }
  return { plans: PLANS, keys: KEYS }
}

/** Attempts today by the tier that served them: the plans' attempts, the keys' fallbacks, and Voice it's own key. */
export function tierToday(plans: LpAccount[], keys: LpAccount[]) {
  return {
    plan: plans.reduce((n, a) => n + a.today, 0),
    key: keys.reduce((n, a) => n + (a.fallbackToday ?? 0), 0),
    voice: 4,
  }
}
