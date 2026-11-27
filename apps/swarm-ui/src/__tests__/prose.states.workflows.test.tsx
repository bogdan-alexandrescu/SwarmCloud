// The states guard's workflows slice: prosestates.tsx visits each state in both themes, the base is its ceiling.
import { afterEach } from 'vitest'

import { proseStateGroup } from './prosestates'
import { WORKFLOWS } from './prosestates.workflows'
import { restoreWorkflowStubs } from './prosestates.workflows.mounts'
import { restoreRunStubs } from './prosestates.workflows.runs'

afterEach(restoreWorkflowStubs)
afterEach(restoreRunStubs)
proseStateGroup('workflows', WORKFLOWS)
