// #2508: in an agent container the worker cap must hold whatever the command
// line says. f-042's agent ran `npx vitest run --pool=forks
// --poolOptions.forks.maxForks=3` in a 5 GiB pod (cap 1), got 3 forks and was
// OOM-killed. This resolves the REAL vitest.config.ts through vitest's node
// API -- the options passed to createVitest are exactly what CLI flags become
// -- and reads the worker count the way vitest 3.2.7's pools read it
// (`poolOptions.forks.maxForks ?? maxWorkers`, see src/vitestWorkers.ts).
//
// IN A CHILD `node`, NOT IN THIS WORKER. The suite's environment is jsdom, and
// esbuild (which bundles the config) refuses to load under jsdom's TextEncoder;
// a node-environment pragma fails instead in setup.ts, which writes
// `window.history` before every test. A child process is also the closest thing
// to the agent's own `npx vitest run`: its environment is exactly the one given
// here, with none of this run's VITEST_* variables.
import { spawnSync } from 'node:child_process'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const ROOT = resolve(__dirname, '../..')
const CONFIG = resolve(ROOT, 'vitest.config.ts')
const TASK_ID = 'task_' + 'b'.repeat(20)
const MARK = 'RESOLVED-CONFIG '

type Workers = {
  pool: string
  maxWorkers?: number
  minWorkers?: number
  poolOptions?: {
    forks?: { maxForks?: number; minForks?: number }
    vmForks?: { maxForks?: number; minForks?: number }
    threads?: { maxThreads?: number; minThreads?: number }
    vmThreads?: { maxThreads?: number; minThreads?: number }
  }
}

// What `vitest.config` holds once createVitest has returned: after the CLI
// options were merged over the file and after every plugin's configureVitest
// hook ran, which is the object a pool is later created from.
function resolved(taskId: string | undefined, cli: Record<string, unknown>): Workers {
  const env: Record<string, string | undefined> = {}
  for (const [k, v] of Object.entries(process.env)) if (!k.startsWith('VITEST')) env[k] = v
  delete env.SWARM_TASK_ID
  if (taskId !== undefined) env.SWARM_TASK_ID = taskId
  const options = { config: CONFIG, root: ROOT, watch: false, run: true, ...cli }
  const script = `
    import { createVitest } from 'vitest/node'
    const vitest = await createVitest('test', ${JSON.stringify(options)})
    const { pool, maxWorkers, minWorkers, poolOptions } = vitest.config
    console.log(${JSON.stringify(MARK)} + JSON.stringify({ pool, maxWorkers, minWorkers, poolOptions }))
    await vitest.close()
  `
  const run = spawnSync(process.execPath, ['--input-type=module', '-e', script], {
    cwd: ROOT,
    env,
    encoding: 'utf8',
    timeout: 25_000,
  })
  const line = run.stdout.split('\n').find((l) => l.startsWith(MARK))
  if (!line) throw new Error(`createVitest printed no config (exit ${run.status}):\n${run.stdout}\n${run.stderr}`)
  return JSON.parse(line.slice(MARK.length)) as Workers
}

// The cap the config computes from this container's own memory limit: an agent
// env with no worker option on the command line resolves maxWorkers to it.
function agentCap(): number {
  const cap = resolved(TASK_ID, {}).maxWorkers
  expect(Number.isInteger(cap) && cap! >= 1).toBe(true)
  return cap!
}

describe('the agent worker cap survives command-line pool options (#2508)', () => {
  it('keeps --pool=forks --poolOptions.forks.maxForks/minForks and --maxWorkers at the cap', () => {
    const cap = agentCap()
    // Above the cap whatever this container's memory: in f-042's 5 GiB pod, 3.
    const asked = cap + 2
    const config = resolved(TASK_ID, {
      pool: 'forks',
      poolOptions: { forks: { maxForks: asked, minForks: asked } },
      maxWorkers: asked,
      minWorkers: asked,
    })
    expect(config.pool).toBe('forks')
    expect(config.poolOptions?.forks?.maxForks ?? config.maxWorkers).toBe(cap)
    expect(config.poolOptions?.forks?.minForks ?? config.minWorkers ?? 0).toBeLessThanOrEqual(cap)
    expect(config.maxWorkers).toBe(cap)
    expect(config.minWorkers ?? 0).toBeLessThanOrEqual(cap)
  })

  it('keeps --pool=threads --poolOptions.threads.maxThreads/minThreads at the cap', () => {
    const cap = agentCap()
    const asked = cap + 2
    const config = resolved(TASK_ID, {
      pool: 'threads',
      poolOptions: { threads: { maxThreads: asked, minThreads: asked } },
    })
    expect(config.poolOptions?.threads?.maxThreads ?? config.maxWorkers).toBe(cap)
    expect(config.poolOptions?.threads?.minThreads ?? config.minWorkers ?? 0).toBeLessThanOrEqual(cap)
  })

  it('keeps the vmForks and vmThreads pools at the cap', () => {
    const cap = agentCap()
    const asked = cap + 2
    const forks = resolved(TASK_ID, { pool: 'vmForks', poolOptions: { vmForks: { maxForks: asked } } })
    expect(forks.poolOptions?.vmForks?.maxForks ?? forks.maxWorkers).toBe(cap)
    const threads = resolved(TASK_ID, { pool: 'vmThreads', poolOptions: { vmThreads: { maxThreads: asked } } })
    expect(threads.poolOptions?.vmThreads?.maxThreads ?? threads.maxWorkers).toBe(cap)
  })

  it('leaves the command line alone outside an agent container', () => {
    for (const taskId of [undefined, '']) {
      const config = resolved(taskId, {
        pool: 'forks',
        poolOptions: { forks: { maxForks: 3, minForks: 3 } },
        maxWorkers: 3,
      })
      expect(config.poolOptions?.forks?.maxForks).toBe(3)
      expect(config.poolOptions?.forks?.minForks).toBe(3)
      expect(config.maxWorkers).toBe(3)
      expect(config.poolOptions?.threads?.maxThreads).toBeUndefined()
    }
  })
})
