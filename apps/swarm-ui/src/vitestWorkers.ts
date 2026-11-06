/**
 * HOW MANY VITEST WORKERS AN AGENT CONTAINER MAY RUN (#1641).
 *
 * vitest's default is one worker per CPU the process can see, and in a GKE pod
 * that is the NODE's count, not the pod's share. A lane running this suite in
 * an 8 GiB `standard` container was OOM-killed that way (task_a092b619d86940c1a5ae,
 * peak RSS pinned at 8 GiB for 20 minutes). Outside an agent container -- CI,
 * which shards the suite four ways, or a laptop -- vitest keeps its default.
 *
 * The worker carries SWARM_TASK_ID into the agent's own environment
 * (`runners/cliagent.py` _PLAIN_PASSTHROUGH, `runners/generic.py` _build_env),
 * and the pre-publish gate's vitest check gets the same environment. Until it
 * did, #1642's gate below was inert: the variable reached the runner process
 * only, and task_45e9d3b7c0264ef78091 was OOM-killed at 8 GiB the same way.
 *
 * The budget: 2 GiB of headroom for the main process and the agent beside it,
 * then 2 GiB per jsdom worker, because the full-app renders are the heavy
 * tests. At least one worker; two when the limit cannot be read.
 *
 * WHY `maxWorkers` ALONE WAS NOT ENOUGH (#2508). f-042's agent ran
 * `npx vitest run --pool=forks --poolOptions.forks.maxForks=3` in a 5 GiB pod,
 * where the cap is 1, got three forks and was OOM-killed; its review and fix
 * steps were swept with it. Read in vitest 3.2.7's own source
 * (node_modules/vitest/dist/chunks/cli-api.*.js and coverage.*.js):
 *
 *  - the command line wins. createVitest's config plugin merges
 *    `deepMerge({}, configDefaults, viteConfig.test, cliOptions)` in
 *    `configResolved`, so `--maxWorkers` or `--poolOptions.*` replaces anything
 *    this file puts in the `test` block -- writing poolOptions into the config
 *    would be overridden exactly as maxWorkers was. VITEST_MAX_FORKS,
 *    VITEST_MAX_THREADS and their MIN_ twins are applied after that, in
 *    resolveConfig.
 *  - each pool takes its own option AHEAD of maxWorkers:
 *      forks      max = poolOptions.forks.maxForks     ?? maxWorkers ?? cpus - 1
 *                 min = poolOptions.forks.minForks     ?? minWorkers ?? min(cpus - 1, max)
 *      threads    the same with poolOptions.threads.maxThreads / minThreads
 *      vmThreads  the same with poolOptions.vmThreads.maxThreads / minThreads
 *      vmForks    max as forks; min = poolOptions.vmForks.MAXForks ?? minWorkers ...
 *                 (sic: 3.2.7 reads maxForks for the min, so the max pins both)
 *  - min above max: Tinypool throws "options.minThreads and options.maxThreads
 *    must not conflict", and where only one is given it RAISES the other to
 *    match. So a min is pinned to at most its pool's max, never left above it.
 *  - every pool is created lazily, from `vitest.config`, on the first run --
 *    after every plugin's `configureVitest` hook has run in `_setServer`. That
 *    hook sees the config with the command line already merged in, so it is
 *    where vitest.config.ts applies `clampPoolOptions` below; the test that
 *    settles this resolves the real config with maxForks above the cap passed
 *    as a CLI option (src/__tests__/vitestWorkers.config.test.ts).
 *
 * The cap is per vitest PROCESS. Four parallel `npx vitest` loops each run at
 * the cap, four times the memory; that is the agent's own doing (#2507), and
 * no config file can see its siblings.
 */
const GIB = 2 ** 30
const HEADROOM = 2 * GIB
const PER_WORKER = 2 * GIB

export function agentMaxWorkers(
  env: Readonly<Record<string, string | undefined>>,
  memoryLimitBytes: number | null,
): number | undefined {
  if (!env.SWARM_TASK_ID) return undefined
  if (memoryLimitBytes === null || !Number.isFinite(memoryLimitBytes) || memoryLimitBytes <= 0) return 2
  return Math.max(1, Math.floor((memoryLimitBytes - HEADROOM) / PER_WORKER))
}

/**
 * HOW MANY VITEST WORKERS A CI SHARD RUNS (part of #2688): half the CPUs this
 * process may run on, at least one; undefined outside CI and in an agent
 * container, where agentMaxWorkers above decides.
 *
 * WHY. "[vitest-worker]: Timeout calling \"onTaskUpdate\"" failed 20 runs in
 * which every test passed, on component shards 2, 4 and 6 of 8. vitest 3.2.7's
 * worker gives every RPC to the main process birpc's fixed 60 s
 * (DEFAULT_TIMEOUT in node_modules/vitest/dist/chunks/index.*.js; the worker's
 * createRuntimeRpc passes no timeout and no config option reaches it), and that
 * one main process answers every worker: each module transform, each console
 * line, each task update. The self-hosted runner is an e2-standard-8 -- 8 vCPU,
 * four physical cores -- where vitest's default of `availableParallelism() - 1`
 * started seven jsdom workers, each a CSS-cascade-bound thread plus V8's GC
 * helpers, so the main process queued behind them past 60 s. The same shard's
 * files reported 485 s for spacing.accounts on CI against 38 s alone in an
 * 8-vCPU agent container: CI was running them about thirteen times slower,
 * which is contention, not work. One worker a physical core leaves the
 * hyperthreads to the main process and the collectors. A longer RPC timeout is
 * not on offer in 3.2.7, and a retry would re-run away the failure this is.
 *
 * GitHub sets CI=true on every runner; `--maxWorkers` on the command line
 * still wins over this, as vitest merges it over the config file.
 */
export function ciMaxWorkers(env: Readonly<Record<string, string | undefined>>, cpus: number): number | undefined {
  if (env.SWARM_TASK_ID) return undefined
  const ci = (env.CI ?? '').trim().toLowerCase()
  if (ci === '' || ci === 'false' || ci === '0') return undefined
  return Math.max(1, Math.floor(cpus / 2))
}

type Workers = number | string | undefined
type ForksOptions = { maxForks?: Workers; minForks?: Workers }
type ThreadsOptions = { maxThreads?: Workers; minThreads?: Workers }
export type PoolWorkerOptions = {
  maxWorkers?: Workers
  minWorkers?: Workers
  poolOptions?: {
    forks?: ForksOptions
    vmForks?: ForksOptions
    threads?: ThreadsOptions
    vmThreads?: ThreadsOptions
  }
}

// A max is at most the cap and is SET even when absent, so no pool falls
// through to a default sized by the node's CPUs. A string here is a percentage
// vitest has not resolved yet; it is replaced, never trusted to be small.
function capped(value: Workers, cap: number): number {
  return typeof value === 'number' && value <= cap ? value : cap
}

// A min stays absent when absent (vitest derives it from the max), else at most
// the max it belongs to: Tinypool throws, or raises the max, when min > max.
function pinned<K extends string>(key: K, value: Workers, max: number): { [P in K]?: number } {
  if (value === undefined) return {}
  return { [key]: typeof value === 'number' ? Math.min(value, max) : max } as { [P in K]?: number }
}

/**
 * The same config with every worker count vitest 3.2.7 reads -- maxWorkers,
 * minWorkers and the max/min of the forks, threads, vmForks and vmThreads pools
 * -- at most `cap`. Undefined cap (outside an agent container): the very same
 * object, untouched. Pure: the input is not modified (#2508).
 */
export function clampPoolOptions<T extends PoolWorkerOptions>(config: T, cap: number | undefined): T {
  if (cap === undefined) return config
  const pools = config.poolOptions ?? {}
  const forks = (o: ForksOptions = {}) => {
    const maxForks = capped(o.maxForks, cap)
    return { ...o, maxForks, ...pinned('minForks', o.minForks, maxForks) }
  }
  const threads = (o: ThreadsOptions = {}) => {
    const maxThreads = capped(o.maxThreads, cap)
    return { ...o, maxThreads, ...pinned('minThreads', o.minThreads, maxThreads) }
  }
  const maxWorkers = capped(config.maxWorkers, cap)
  return {
    ...config,
    maxWorkers,
    ...pinned('minWorkers', config.minWorkers, maxWorkers),
    poolOptions: {
      ...pools,
      forks: forks(pools.forks),
      vmForks: forks(pools.vmForks),
      threads: threads(pools.threads),
      vmThreads: threads(pools.vmThreads),
    },
  }
}
