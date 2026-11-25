// The overview section's click-only states (issue 439; the harness and the rule
// are in prosestates.tsx). Capacity's profiles: one profile's row expanded.
// The section's own step registers the rest and takes its files out of PENDING.

import type { ProseState } from './prosestates'
import { click } from './prosestates.shell'

export const OVERVIEW: readonly ProseState[] = [
  {
    key: 'capacity/profile-row',
    group: 'overview',
    files: ['ProfileMatrix.tsx'],
    shows: '.cap-mx-exp',
    walk: { open: 'capacity/profiles', then: () => click('.cap-mx-toggle') },
  },
]
