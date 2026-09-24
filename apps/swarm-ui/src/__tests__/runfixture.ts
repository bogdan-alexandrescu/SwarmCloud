// Builders for the inspector-chart tests: a task, an attempt, an event.
//
// Every field is the API's own shape (`task_to_api`, `attempt_to_api`,
// `_event_to_api`), filled with the dull value, so each test states ONLY the
// fields its claim is about. A fixture that set a dozen interesting fields in
// every test would make it impossible to see which one a test depends on.

import type { AttemptRow, Task, TaskEvent } from '../types'

export const MIN = 60_000

/** An ISO instant `m` minutes (fractions allowed) after 2026-09-22T10:00:00Z. */
export function at(m: number): string {
  return new Date(Date.UTC(2026, 8, 22, 10, 0, 0) + m * MIN).toISOString()
}

export function task(over: Partial<Task> = {}): Task {
  return {
    id: 'tsk_charts',
    tenant_id: 'acme',
    state: 'SUCCEEDED',
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 5,
    created_at: at(-1),
    updated_at: at(60),
    started_at: null,
    completed_at: null,
    submitted_by: null,
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: null,
    next_eligible_at: null,
    metadata: null,
    repository_ref: null,
    input: null,
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: null,
    ...over,
  }
}

export function attempt(n: number, over: Partial<AttemptRow> = {}): AttemptRow {
  return {
    attempt_id: `att_${n}`,
    task_id: 'tsk_charts',
    tenant_id: 'acme',
    generation: n,
    lease_id: `lse_${n}`,
    backend: 'CLOUD_RUN_JOB',
    execution_name: `exec-${n}`,
    created_at: at(0),
    started_at: at(1),
    completed_at: at(10),
    exit_code: 0,
    error: null,
    peak_rss_bytes: null,
    peak_disk_bytes: null,
    oom_near_miss: false,
    checkpoints: [],
    input_tokens: null,
    output_tokens: null,
    cache_read_input_tokens: null,
    cache_creation_input_tokens: null,
    cost_usd: null,
    ...over,
  }
}

let seq = 0

export function ev(
  type: string,
  when: string,
  attemptId: string | null,
  detail: Record<string, unknown> | null = null,
): TaskEvent {
  seq += 1
  return {
    event_id: `ev_${seq}`,
    task_id: 'tsk_charts',
    type,
    at: when,
    attempt_id: attemptId,
    lease_id: null,
    generation: null,
    detail,
  }
}
