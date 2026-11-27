// The states guard's config slice: prosestates.tsx visits each state in both themes, the base is its ceiling.
import { afterEach } from 'vitest'

import { proseStateGroup } from './prosestates'
import { restoreOpenedStubs } from './prosestates.opened.mounts'
import { CONFIG } from './prosestates.config'
import { restoreConfigStubs } from './prosestates.config.mounts'

afterEach(restoreConfigStubs)

afterEach(restoreOpenedStubs)
proseStateGroup('config', CONFIG)
