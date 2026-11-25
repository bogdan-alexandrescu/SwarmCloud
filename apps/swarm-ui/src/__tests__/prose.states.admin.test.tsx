// The states guard's admin slice: prosestates.tsx visits each state in both themes, the base is its ceiling.
import { proseStateGroup } from './prosestates'
import { ADMIN } from './prosestates.admin'

proseStateGroup('admin', ADMIN)
