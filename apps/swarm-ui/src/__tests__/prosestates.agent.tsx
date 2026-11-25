// The agent section's click-only states (issue 439; the harness and the rule
// are in prosestates.tsx). An agent page: the Resources fold opened.
// The section's own step registers the rest and takes its files out of PENDING.

import type { ProseState } from './prosestates'
import { click } from './prosestates.shell'

export const AGENT: readonly ProseState[] = [
  {
    key: 'agent/resources',
    group: 'agent',
    files: ['AgentDetail.tsx'],
    shows: 'details.dt-res[open]',
    walk: { open: 'work/task/task_a073aff5', then: () => click('details.dt-res > summary') },
  },
]
