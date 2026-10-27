// The sc plugin's state contract: the values its hooks module (hooks/pane.tsx,
// with hooks/band.tsx) keeps in `$.state` -- the SwarmCloud pane's browsing,
// which the band above the prompt draws too, and the band's own -- so a hot
// reload keeps what was last read. `claude plugin validate` holds every `$.state` key the module
// names to this file. Shapes are swarm_browse's answers (apps/swarm-mcp/
// swarm_mcp/browse.py), kept as the bridge served them: a figure nobody
// recorded is null, never 0.

export type ScStep = {
  step_id: string; task_id?: string | null; depends_on?: string[]; state?: string | null; park_reason?: string
  /** Why a HUMAN_REQUIRED step holds, where swarm_browse serves it: `owner_questions` is a merge held by them. */
  park_phase?: string
}

export type ScWorkflowRow = {
  workflow_id: string
  name: string
  state: string | null
  steps: ScStep[]
  duration_s?: number | null
  tokens?: number | null
  cost_usd?: number | null
  console?: string
  read_error?: string
  /** The pull requests its finished steps opened, where swarm_browse serves them (the band counts and names them). */
  pull_requests?: string[]
  /** The owner questions its steps' agents asked, where swarm_browse serves the count (the band notices a new one). */
  owner_questions?: number
}

export type ScSingleTask = { task_id: string; label?: string | null; state?: string | null; age_seconds?: number | null }

export type ScAgentRow = { task_id: string; step_id?: string | null; state: string | null; note?: string }

export type ScAgentDetail = {
  task_id: string
  step_id?: string | null
  state: string | null
  park_reason?: string | null
  attempts?: number | null
  max_attempts?: number | null
  duration_s?: number | null
  duration_basis?: string
  cost_usd?: number | null
  tokens?: number | null
  last_error?: string | null
  pr_url?: string | null
  artifacts?: string[]
  console?: string
}

export type ScLayout = 'rows' | 'pane' | 'band'
export type ScLevel = 'workflows' | 'stages' | 'agents' | 'agent' | 'settings'

/** A read that failed: which tool, why, when, and the action Retry runs again. */
export type ScFailure = { tool: string; reason: string; at: string; action: string }

export type ScPaneState = {
  level: ScLevel
  workflows: ScWorkflowRow[] | null
  singles: ScSingleTask[]
  listReadAt: string | null
  workflowId: string | null
  workflow: ScWorkflowRow | null
  workflowReadAt: string | null
  stage: number | null
  agents: ScAgentRow[] | null
  agentsReadAt: string | null
  taskId: string | null
  agent: ScAgentDetail | null
  agentReadAt: string | null
  agentFrom: 'agents' | 'workflows'
  layout: ScLayout | null
  layoutNote: string | null
  failure: ScFailure | null
  calls: number
  /**
   * Whether Level 1 lists this PROJECT's running work beside this session's
   * (`swarm_browse {level: "workflows", project: true}`): set at session start,
   * which tracks the project's running work (SC-BROWSER-FOLLOWUP).
   */
  project: boolean
  /** Whether this session's start already asked for the layout: one swarm_layout call per session, none on a reload. */
  started: boolean
}

/**
 * The band above the prompt (SC-BAND, BGD 2026-10-26): whether it is on (the
 * project's layout is `band`), folded to its one summary line, and what the
 * last list read showed of each workflow, so the next read queues a chat turn
 * only for what changed.
 */
export type ScBandState = {
  on: boolean
  folded: boolean
  /** Per workflow id, the facts a turn is queued for (`noticesOf`); null until the first read, which queues nothing. */
  seen: Record<string, string[]> | null
  /** The summary line as last drawn, `SwarmCloud · 6 running · 1 waits on you · 2 PRs`. */
  summary: string | null
  /** Chat turns this band queued in this session. */
  turns: number
}

declare module 'claude-code' {
  interface PluginState {
    sc: { pane: ScPaneState; band: ScBandState }
  }
}
