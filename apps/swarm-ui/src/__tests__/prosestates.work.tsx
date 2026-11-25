// The work section's click-only states (issue 439; the harness and the rule
// are in prosestates.tsx). The timeline: its key opened.
// The section's own step registers the rest and takes its files out of PENDING.

import type { ProseState } from './prosestates'
import { click } from './prosestates.shell'

export const WORK: readonly ProseState[] = [
  {
    key: 'work/timeline-key',
    group: 'work',
    files: ['TimelineLanes.tsx'],
    shows: '.tl-key.is-open',
    walk: { open: 'work/timeline', then: () => click('.tl-key-toggle') },
  },
]
