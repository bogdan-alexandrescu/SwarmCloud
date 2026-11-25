// The states guard's agent slice: prosestates.tsx visits each state in both themes, the base is its ceiling.
import { afterEach, vi } from 'vitest'

import { proseStateGroup } from './prosestates'
import { AGENT } from './prosestates.agent'
import { refuse, restoreAgentStubs } from './prosestates.agent.mounts'

// A stop the API refuses (agent/stop-refused): the build's own cancelTask otherwise.
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, cancelTask: (id: string) => (refuse.cancel === null ? actual.cancelTask(id) : Promise.resolve(refuse.cancel)) }
})

afterEach(restoreAgentStubs)
proseStateGroup('agent', AGENT)
