// The states guard's workflows slice: prosestates.tsx visits each state in both themes, the base is its ceiling.
import { proseStateGroup } from './prosestates'
import { WORKFLOWS } from './prosestates.workflows'

proseStateGroup('workflows', WORKFLOWS)
