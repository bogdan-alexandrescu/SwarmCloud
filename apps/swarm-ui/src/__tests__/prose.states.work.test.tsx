// The states guard's work slice: prosestates.tsx visits each state in both themes, the base is its ceiling.
import { afterEach } from 'vitest'

import { proseStateGroup } from './prosestates'
import { WORK } from './prosestates.work'
import { restoreWorkStubs } from './prosestates.work.mounts'

afterEach(restoreWorkStubs)

proseStateGroup('work', WORK)
