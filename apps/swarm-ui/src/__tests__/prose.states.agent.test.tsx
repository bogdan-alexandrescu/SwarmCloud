// The states guard's agent slice: prosestates.tsx visits each state in both themes, the base is its ceiling.
import { afterEach, vi } from 'vitest'

import { proseStateGroup } from './prosestates'
import { AGENT } from './prosestates.agent'
import { refuse, restoreAgentStubs, served } from './prosestates.agent.mounts'

// A stop the API refuses (agent/stop-refused), and a transcript window it refuses: the build's own answers otherwise.
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    cancelTask: (id: string) => (refuse.cancel === null ? actual.cancelTask(id) : Promise.resolve(refuse.cancel)),
    // The transcript's next window refused (agent/transcript-next-refused).
    loadTranscript: (...args: Parameters<typeof actual.loadTranscript>) =>
      refuse.transcript === null ? actual.loadTranscript(...args) : Promise.resolve(refuse.transcript),
    // A merge step's re-checks (agent/earlier-attempts).
    loadAttempts: (id: string) => (served.attempts === null ? actual.loadAttempts(id) : Promise.resolve(served.attempts)),
    loadAgentDetail: (id: string) => (served.detail === null ? actual.loadAgentDetail(id) : Promise.resolve(served.detail)),
  }
})

afterEach(restoreAgentStubs)
proseStateGroup('agent', AGENT)
