// The states guard's config slice: prosestates.tsx visits each state in both themes, the base is its ceiling.
import { proseStateGroup } from './prosestates'
import { CONFIG } from './prosestates.config'

proseStateGroup('config', CONFIG)
