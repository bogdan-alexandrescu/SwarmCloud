// THE TRACE'S TEST RECORDS: stream-json records, as the CLI writes them,
// mapped to steps the way swarm-api's `map_event` (swarm_api/transcript.py)
// maps them with `include_raw=true` -- one step per content block, each
// carrying its whole record as `raw`. Nothing here is drawn by the product.

import type { TaskTranscript, TranscriptStep } from '../types'

export const CWD = '/workspace/att_0f1e2d3c/work/repo'

type Rec = Record<string, unknown>

/** The steps `map_event` makes of a list of records, numbered by line. */
export function stepsOf(records: Rec[]): TranscriptStep[] {
  const out: TranscriptStep[] = []
  records.forEach((r, line) => {
    const raw = JSON.stringify(r)
    const base = (block: number, over: Partial<TranscriptStep>): TranscriptStep => ({
      id: `L${line * 100}:${block}`,
      line_offset: line * 100,
      block,
      kind: 'other',
      role: null,
      parent_tool_use_id: null,
      text: null,
      tool: null,
      tool_result: null,
      meta: null,
      truncated_fields: [],
      raw,
      ...over,
    })
    const type = r.type
    if (type === 'system' && r.subtype === 'init') {
      out.push(base(0, { kind: 'init', role: 'system', meta: { model: r.model, permission_mode: r.permissionMode, tool_count: Array.isArray(r.tools) ? r.tools.length : null } }))
    } else if (type === 'system') {
      out.push(base(0, { kind: 'system', role: 'system', meta: { subtype: r.subtype } }))
    } else if (type === 'assistant' || type === 'user') {
      const blocks = ((r.message as Rec).content as Rec[]) ?? []
      blocks.forEach((b, i) => {
        const role = type
        if (b.type === 'text') out.push(base(i, { kind: 'text', role, text: b.text as string }))
        else if (b.type === 'thinking') out.push(base(i, { kind: 'thinking', role, text: b.thinking as string }))
        else if (b.type === 'redacted_thinking') out.push(base(i, { kind: 'thinking', role, meta: { redacted: true } }))
        else if (b.type === 'tool_use') {
          out.push(base(i, { kind: 'tool_call', role, tool: { id: b.id as string, name: b.name as string, input: JSON.stringify(b.input, null, 2) } }))
        } else if (b.type === 'tool_result') {
          const content = b.content
          const text = typeof content === 'string' ? content : (content as Rec[]).filter((c) => c.type === 'text').map((c) => c.text).join('\n')
          const images = typeof content === 'string' ? 0 : (content as Rec[]).filter((c) => c.type === 'image').length
          out.push(base(i, { kind: 'tool_result', role, tool_result: { tool_use_id: b.tool_use_id as string, is_error: typeof b.is_error === 'boolean' ? b.is_error : null, content: text, images } }))
        }
      })
    } else if (type === 'result') {
      out.push(base(0, { kind: 'result', text: (r.result as string) ?? null, meta: { subtype: r.subtype, is_error: r.is_error, num_turns: r.num_turns, duration_ms: r.duration_ms, total_cost_usd: r.total_cost_usd } }))
    } else if (type === 'rate_limit_event') {
      const info = r.rate_limit_info as Rec
      out.push(base(0, { kind: 'rate_limit', meta: Object.fromEntries(Object.entries(info).map(([k, v]) => [`rate_limit_info.${k}`, v])) }))
    } else {
      out.push(base(0, { kind: 'other', meta: { raw_type: type } }))
    }
  })
  return out
}

export const init = (): Rec => ({ type: 'system', subtype: 'init', cwd: CWD, model: 'claude-opus-5', permissionMode: 'bypassPermissions', tools: ['Bash', 'Read', 'Edit'] })
export const say = (text: string, usage?: Rec): Rec => ({ type: 'assistant', message: { content: [{ type: 'text', text }], ...(usage ? { usage } : {}) } })
export const call = (id: string, name: string, input: Rec): Rec => ({ type: 'assistant', message: { content: [{ type: 'tool_use', id, name, input }] } })
export const reply = (id: string, content: string, isError = false, extra: Rec = {}): Rec => ({
  type: 'user',
  message: { content: [{ type: 'tool_result', tool_use_id: id, content, is_error: isError }] },
  ...extra,
})
export const progress = (id: string, secs: number): Rec => ({ type: 'tool_progress', tool_use_id: id, tool_name: 'Bash', elapsed_time_seconds: secs })
export const result = (text: string, over: Rec = {}): Rec => ({ type: 'result', subtype: 'success', is_error: false, num_turns: 7, duration_ms: 124_000, total_cost_usd: 1.234, result: text, ...over })

/** A finished coding lane: read, edit, a failing test run, a passing one, an unknown record, its answer and its result. */
export function codingRecords(): Rec[] {
  const answer = '## Done\nThe reconciler now drops a stale generation.'
  return [
    init(),
    say('Reading the **contract** before changing anything.', { output_tokens: 42 }),
    call('t1', 'Read', { file_path: `${CWD}/CONTRACT.md` }),
    reply('t1', '# The contract\n...'),
    call('t2', 'Edit', { file_path: `${CWD}/apps/reconciler.py`, old_string: 'b', new_string: 'c' }),
    reply('t2', 'The file was updated.', false, { tool_use_result: { structuredPatch: [{ oldStart: 10, newStart: 10, lines: [' a', '-b', '+c', ' d'] }] } }),
    call('t3', 'Bash', { command: `cd ${CWD} && uv run pytest tests/unit/test_reconciler.py -q`, description: 'Run the related tests' }),
    progress('t3', 4),
    progress('t3', 12),
    reply('t3', 'collected 12 items\n\n>       assert gen == 2\nE       assert 1 == 2\nFAILED tests/unit/test_reconciler.py::test_stale\n1 failed, 11 passed in 0.31s', true),
    call('t4', 'Bash', { command: `cd ${CWD} && uv run pytest tests/unit/test_reconciler.py -q` }),
    reply('t4', '12 passed in 0.22s'),
    { type: 'assistant', message: { content: [{ type: 'redacted_thinking', data: 'x' }] } },
    { type: 'auth_status', isAuthenticating: false },
    call('t5', 'Write', { file_path: '/tmp/pr-body.md', content: 'body\n' }),
    reply('t5', 'File created.'),
    say(answer),
    result(answer),
  ]
}

export function transcriptOf(steps: TranscriptStep[], over: Partial<TaskTranscript> = {}): TaskTranscript {
  return {
    task_id: 'task_trace',
    tenant_id: 'eng',
    attempt_id: 'att_1',
    attempt: { status: 'latest', known: true, generation: 1, created_at: new Date(Date.now() - 60_000).toISOString(), completed_at: null, exit_code: null },
    read_at: new Date().toISOString(),
    stream: {
      stream: 'agent_stdout',
      source: 'final',
      status: 'ok',
      detail: null,
      uri: null,
      object_updated_at: new Date().toISOString(),
      age_seconds: 3,
      total_bytes: 4000,
      offset: 0,
      returned_bytes: 4000,
      next_offset: null,
      truncated: false,
      tail_window: null,
    },
    format: 'claude-stream-json',
    steps,
    complete: true,
    window_starts_mid_stream: false,
    skipped_lines: 0,
    answer_in_window: true,
    redaction: { applied_at_read_time: true, rules: 12 },
    redaction_count: 0,
    ...over,
  } as TaskTranscript
}
