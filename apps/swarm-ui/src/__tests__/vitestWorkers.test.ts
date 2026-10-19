// #1641: a lane running this suite in an 8 GiB agent container was OOM-killed,
// because vitest started one jsdom worker per CPU of the NODE, not of the pod.
// In an agent container (SWARM_TASK_ID set) the worker count is sized to the
// container's memory; outside one (CI, a laptop) vitest keeps its own default.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { agentMaxWorkers } from '../vitestWorkers'

const GIB = 2 ** 30
const IN_AGENT = { SWARM_TASK_ID: 'task_' + 'a'.repeat(20) }

describe('agentMaxWorkers (#1641)', () => {
  it('leaves vitest its default outside an agent container', () => {
    expect(agentMaxWorkers({}, 8 * GIB)).toBeUndefined()
    expect(agentMaxWorkers({ SWARM_TASK_ID: '' }, 8 * GIB)).toBeUndefined()
  })

  it('gives an 8 GiB agent container three workers: 2 GiB each after 2 GiB of headroom', () => {
    expect(agentMaxWorkers(IN_AGENT, 8 * GIB)).toBe(3)
  })

  it('never goes below one worker, however small the limit', () => {
    expect(agentMaxWorkers(IN_AGENT, 2 * GIB)).toBe(1)
    expect(agentMaxWorkers(IN_AGENT, 512 * 2 ** 20)).toBe(1)
  })

  it('uses two workers when the container limit cannot be read', () => {
    expect(agentMaxWorkers(IN_AGENT, null)).toBe(2)
    expect(agentMaxWorkers(IN_AGENT, Number.NaN)).toBe(2)
  })

  it('is what vitest.config.ts actually passes as maxWorkers', () => {
    const config = readFileSync(resolve(__dirname, '../../vitest.config.ts'), 'utf8')
    expect(config).toMatch(/maxWorkers:\s*agentMaxWorkers\(process\.env,/)
    expect(config).toContain('/sys/fs/cgroup/memory.max')
  })
})
