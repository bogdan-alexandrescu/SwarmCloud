// The sc plugin's state contract: the one value its hooks module (hooks/pane.tsx)
// keeps in `$.state`, the SwarmCloud pane's, so a hot reload keeps what the
// pane last read. `claude plugin validate` holds every `$.state` key the module
// names to this file. Shapes are swarm_browse's answers (apps/swarm-mcp/
// swarm_mcp/browse.py), kept as the bridge served them: a figure nobody
// recorded is null, never 0.

export type ScStep = { step_id: string; task_id?: string | null; depends_on?: string[]; state?: string | null; park_reason?: string }

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

export type ScLayout = 'rows' | 'pane'
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

declare module 'claude-code' {
  interface PluginState {
    sc: { pane: ScPaneState }
  }
}
