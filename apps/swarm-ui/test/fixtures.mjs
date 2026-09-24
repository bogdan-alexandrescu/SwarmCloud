// Builders for the shapes `deriveChecks` is given. Nothing here is a mock of
// the logic under test -- these produce the same objects the API's codecs
// produce, so a field renamed on the server breaks the TypeScript rather than
// being silently absent from a hand-written literal.

/** A loading Result, for the checks that are not the subject of a test. */
export const loading = { status: 'loading', since: 0 }

export function ok(data, fetchedAt = 0) {
  return { status: 'ok', data, fetchedAt }
}

export function empty(fetchedAt = 0) {
  return { status: 'empty', fetchedAt }
}

export function failed(kind = 'server_error', message = 'boom') {
  return { status: 'error', error: { kind, httpStatus: 500, code: null, message } }
}

export const NOW = Date.parse('2026-09-22T12:00:00Z')

export function agoIso(seconds, now = NOW) {
  return new Date(now - seconds * 1000).toISOString()
}

/**
 * One workflow as `workflow_to_api` sends it on the LIST route.
 *
 * `counts` is what the caller passes; everything derivable from it is derived
 * here, the way `swarm_api.rollup.derive` derives it, so a fixture cannot
 * describe a workflow the platform could not produce.
 */
export function workflow({
  id = 'wf_test',
  counts = {},
  quietSeconds = 0,
  complete = true,
  state = null,
  cancelRequested = false,
  updatedAt = null,
  now = NOW,
} = {}) {
  const derived = state ?? (complete ? 'RUNNING' : 'UNKNOWN')
  return {
    workflow_id: id,
    state: derived,
    tenant_id: 'u-test',
    stored_state: derived,
    state_source: 'derived',
    rollup: {
      state: derived,
      complete,
      reason: complete ? 'steps_hold_capacity' : 'step_tasks_unread',
      counts,
      unreadable_steps: complete ? [] : ['step-2'],
      unstarted_steps: [],
      steps_read: Object.values(counts).reduce((a, b) => a + b, 0),
    },
    created_at: agoIso(quietSeconds + 60, now),
    updated_at: updatedAt ?? agoIso(quietSeconds, now),
    submitted_by: 'someone@saga.xyz',
    priority: 0,
    on_step_failure: 'FAIL_WORKFLOW',
    cancel_requested: cancelRequested,
    steps: [],
  }
}

export function workflowPage(workflows, report = null) {
  return {
    workflows,
    next_page_token: null,
    tenant_id: 'u-test',
    ...(report ? { rollup_report: report } : {}),
  }
}

/** One task as `task_to_api` sends it. Only the fields the checks read vary. */
export function task({
  id = 'task_test',
  state = 'READY',
  parkReason = null,
  nextEligibleAt = null,
  workflowId = null,
  stepId = null,
  attempts = 0,
  maxAttempts = 3,
  lastError = null,
  now = NOW,
} = {}) {
  return {
    id,
    tenant_id: 'u-test',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: new Date(now - 600_000).toISOString(),
    updated_at: new Date(now - 60_000).toISOString(),
    started_at: null,
    completed_at: null,
    submitted_by: 'someone@saga.xyz',
    attempt_count: attempts,
    max_attempts: maxAttempts,
    park_reason: parkReason,
    blocked_by: null,
    workflow_id: workflowId,
    step_id: stepId,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: null,
    next_eligible_at: nextEligibleAt,
    metadata: null,
    repository_ref: null,
    input: {},
    last_error: lastError,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: null,
  }
}

export function taskPage(tasks) {
  return { tasks, next_page_token: null, tenant_id: 'u-test' }
}

/**
 * Every read `deriveChecks` takes, with the ones a test does not care about
 * left `loading` -- which produces `reading`, never `clear`. A fixture that
 * defaulted them to empty would let a test claim an all-clear it never earned.
 */
export function inputs(overrides = {}) {
  return {
    capacity: loading,
    tasks: loading,
    leases: loading,
    providers: loading,
    accounts: loading,
    stats: loading,
    workflows: loading,
    ...overrides,
  }
}

/** The check with this label, or a failure naming the labels that exist. */
export function checkNamed(checks, label) {
  const found = checks.find((c) => c.label === label)
  if (!found) {
    throw new Error(
      `no check labelled ${JSON.stringify(label)}; deriveChecks returned ` +
        JSON.stringify(checks.map((c) => c.label)),
    )
  }
  return found
}

/** Every problem across every check, which is what the panel actually draws. */
export function problems(checks) {
  return checks.flatMap((c) => (c.status === 'found' ? c.problems : []))
}
