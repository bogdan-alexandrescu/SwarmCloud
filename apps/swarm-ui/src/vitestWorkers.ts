/**
 * HOW MANY VITEST WORKERS AN AGENT CONTAINER MAY RUN (#1641).
 *
 * vitest's default is one worker per CPU the process can see, and in a GKE pod
 * that is the NODE's count, not the pod's share. A lane running this suite in
 * an 8 GiB `standard` container was OOM-killed that way (task_a092b619d86940c1a5ae,
 * peak RSS pinned at 8 GiB for 20 minutes). Outside an agent container -- CI,
 * which shards the suite four ways, or a laptop -- vitest keeps its default.
 *
 * The budget: 2 GiB of headroom for the main process and the agent beside it,
 * then 2 GiB per jsdom worker, because the full-app renders are the heavy
 * tests. At least one worker; two when the limit cannot be read.
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
