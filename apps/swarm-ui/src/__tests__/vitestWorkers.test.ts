// #1641: a lane running this suite in an 8 GiB agent container was OOM-killed,
// because vitest started one jsdom worker per CPU of the NODE, not of the pod.
// In an agent container (SWARM_TASK_ID set) the worker count is sized to the
// container's memory; outside one (CI, a laptop) vitest keeps its own default.
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { agentMaxWorkers, clampPoolOptions } from '../vitestWorkers'

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
    expect(config).toMatch(/const agentCap = agentMaxWorkers\(process\.env, containerMemoryLimit\(\)\)/)
    expect(config).toMatch(/maxWorkers:\s*agentCap,/)
    // #2508: and clamps the resolved config with it, after the command line.
    expect(config).toMatch(/configureVitest\(\{ vitest \}\) \{\s*Object\.assign\(vitest\.config, clampPoolOptions\(vitest\.config, agentCap\)\)/)
    expect(config).toContain('/sys/fs/cgroup/memory.max')
  })
})

// #2508: a command-line --poolOptions.forks.maxForks=3 beat maxWorkers: 1, because
// each vitest pool reads its own option ahead of maxWorkers. The clamp covers
// every count a 3.2.7 pool reads; vitestWorkers.config.test.ts proves it is
// applied after the command line is merged.
describe('clampPoolOptions (#2508)', () => {
  it('caps the max of every pool and maxWorkers, setting them when absent', () => {
    expect(clampPoolOptions({}, 2)).toEqual({
      maxWorkers: 2,
      poolOptions: {
        forks: { maxForks: 2 },
        vmForks: { maxForks: 2 },
        threads: { maxThreads: 2 },
        vmThreads: { maxThreads: 2 },
      },
    })
    const asked = {
      maxWorkers: 3,
      poolOptions: {
        forks: { maxForks: 3, singleFork: false },
        vmForks: { maxForks: 3 },
        threads: { maxThreads: 3 },
        vmThreads: { maxThreads: 3, memoryLimit: '1G' },
      },
    }
    const out = clampPoolOptions(asked, 1)
    expect(out.maxWorkers).toBe(1)
    expect(out.poolOptions.forks).toEqual({ maxForks: 1, singleFork: false })
    expect(out.poolOptions.vmForks).toEqual({ maxForks: 1 })
    expect(out.poolOptions.threads).toEqual({ maxThreads: 1 })
    expect(out.poolOptions.vmThreads).toEqual({ maxThreads: 1, memoryLimit: '1G' })
    // Pure: the caller's object is not touched.
    expect(asked.poolOptions.forks.maxForks).toBe(3)
    expect(asked.maxWorkers).toBe(3)
  })

  it('keeps a value already under the cap', () => {
    const out = clampPoolOptions({ maxWorkers: 1, poolOptions: { forks: { maxForks: 2, minForks: 1 } } }, 3)
    expect(out.maxWorkers).toBe(1)
    expect(out.poolOptions.forks).toEqual({ maxForks: 2, minForks: 1 })
  })

  it('pins every min to at most its max, and leaves an absent min absent', () => {
    const out = clampPoolOptions(
      {
        minWorkers: 4,
        poolOptions: {
          forks: { maxForks: 4, minForks: 4 },
          vmForks: { minForks: 4 },
          threads: { maxThreads: 4, minThreads: 4 },
          vmThreads: { minThreads: 4 },
        },
      },
      1,
    )
    expect(out.minWorkers).toBe(1)
    expect(out.poolOptions.forks).toEqual({ maxForks: 1, minForks: 1 })
    expect(out.poolOptions.vmForks).toEqual({ maxForks: 1, minForks: 1 })
    expect(out.poolOptions.threads).toEqual({ maxThreads: 1, minThreads: 1 })
    expect(out.poolOptions.vmThreads).toEqual({ maxThreads: 1, minThreads: 1 })
    expect(clampPoolOptions({}, 1)).not.toHaveProperty('minWorkers')
    expect(clampPoolOptions({}, 1)).not.toHaveProperty('poolOptions.forks.minForks')
    expect(clampPoolOptions({}, 1)).toHaveProperty('poolOptions.forks.maxForks', 1)
  })

  it('replaces an unresolved percentage rather than trusting it to be small', () => {
    const out = clampPoolOptions({ maxWorkers: '100%', poolOptions: { forks: { maxForks: '50%', minForks: '50%' } } }, 2)
    expect(out.maxWorkers).toBe(2)
    expect(out.poolOptions.forks).toEqual({ maxForks: 2, minForks: 2 })
  })

  it('is a no-op outside an agent container: the very same object, untouched', () => {
    const config = { maxWorkers: 8, poolOptions: { forks: { maxForks: 8, minForks: 8 } } }
    const snapshot = JSON.parse(JSON.stringify(config))
    expect(clampPoolOptions(config, undefined)).toBe(config)
    expect(config).toEqual(snapshot)
    expect(clampPoolOptions(config, agentMaxWorkers({}, 8 * GIB))).toBe(config)
  })
})
