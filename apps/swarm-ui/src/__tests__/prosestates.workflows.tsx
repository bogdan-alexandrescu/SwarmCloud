// The workflows section's click-only states (issue 439; the harness and the rule
// are in prosestates.tsx). A workflow's table: a step's inputs opened.
// The section's own step registers the rest and takes its files out of PENDING.

import type { ProseState } from './prosestates'
import { click } from './prosestates.shell'

export const WORKFLOWS: readonly ProseState[] = [
  {
    key: 'workflow/step-inputs',
    group: 'workflows',
    files: ['WorkflowViews.tsx'],
    shows: '.wf-xinputs',
    walk: { open: '/workflows/wf_5e5ad3b6f7da4299a839/table', then: () => click('.wf-inputs-toggle') },
  },
]
