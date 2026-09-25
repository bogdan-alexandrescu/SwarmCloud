// AG-23 (#82). THE CHECKPOINT AND LOG PANELS, FLATTENED ONTO THE SHARED PRIMITIVES.
//
// THE DEFECT, measured on the live inspector on 2026-09-25. Both panels were
// boxes inside boxes: a `.ckpt` bordered card per checkpoint and a `.logwin`
// bordered card per stream, each holding a legacy `dl.kv` of five
// label/value rows, a `<details>` with a second `dl.kv`, and sentences
// between them. The measured-empty stream said so in a paragraph ("This
// object exists and is empty ... a real, measured empty stream, not a failed
// read") where the kit has a mark for exactly that, and the pointer line put
// the server's lowercase detail after a full stop.
//
// THE OWNER'S DECISION, 2026-09-25: one level of box; files as a table (name,
// size, age); zero, absent and partial as the kit's marks; keep only the
// sentences that state a fact the table cannot show.
//
// So, per panel: a facts strip, ONE `.ctl-table` (the one box, §13.3), and a
// kit mark in the cell whose value is a kind of nothing. What stays as a
// sentence is what no cell can say: a checkpoint written and since reclaimed,
// and a restore pointer that names nothing the listing holds.
//
// MUTATION, each one a case below: put a `.ckpt` or `.logwin` box back, a
// `dl.kv` back, the empty-stream paragraph back, `resumable ?? false`, or the
// `.` before the pointer's detail.
//
// THE SECOND PASS (fix-up on #170). Four things the first pass got wrong, each
// a case below:
//
//   * THREE COLUMNS, AS DECIDED. The checkpoint table had a fourth, Resume.
//     Whether a retry would restore from a checkpoint is now a line under its
//     name, and a checkpoint's objects -- the files it is stored as -- are
//     rows of the same table with a name, a size and an age, not a disclosure
//     of `name · size` spans.
//   * THE RESTORE FACT SAYS WHAT THE WORKER WOULD DO. It printed the pointer
//     for `missing` and `outside_this_task`, and "a retry starts from the
//     beginning" for `unset`. `_restore_checkpoint` (agent_worker/lifecycle.py)
//     tries the pointer, and when that resolves to nothing -- a pointer outside
//     the task's prefix, a checkpoint no longer there, no pointer at all --
//     falls back to `find_latest`, the newest committed checkpoint it can read.
//     So a task with a resumable checkpoint listed never restarts from nothing
//     because of its pointer, and the panel said it would.
//   * `live` ONLY WHILE SOMETHING IS WRITING. `source=auto` serves the live
//     tail when the final log is absent, which is exactly what a worker killed
//     before its upload leaves behind; the table called that tail `live` for
//     ever after.
//   * ONE SENTENCE, ONCE. With no attempt the route still names both streams,
//     each carrying "this task has no attempt yet", so the fact was said three
//     times: the strip, then once per row.

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { CheckpointRecord, CheckpointsPage, LogStream, Task, TaskLogs } from '../types'
import { attempt, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { RunFiles } from '../RunFiles'

const PREFIX = 'tenants/acme/tasks/tsk_files/checkpoints'
const NOW = Date.now()
const iso = (minutesAgo: number) => new Date(NOW - minutesAgo * 60_000).toISOString()

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: NOW, serverAt: new Date(NOW).toISOString() }
}

function record(id: string, over: Partial<CheckpointRecord> = {}): CheckpointRecord {
  return {
    checkpoint_id: id,
    attempt_id: 'att_1',
    attempt_known: true,
    attempt_created_at: iso(30),
    attempt_completed_at: null,
    prefix: `${PREFIX}/${id}`,
    uri: `gs://swarm-workspaces/${PREFIX}/${id}`,
    is_latest_pointer: false,
    objects: [
      { name: 'manifest.json', key: `${PREFIX}/${id}/manifest.json`, bytes: 412 },
      { name: 'workspace.tar.zst', key: `${PREFIX}/${id}/workspace.tar.zst`, bytes: 18_442_240 },
    ],
    stored_bytes: 18_442_652,
    manifest: 'present',
    manifest_detail: null,
    created_at: iso(4),
    seq: 2,
    generation: 1,
    label: null,
    archive_bytes: 18_442_240,
    archive_sha256: null,
    file_count: 184,
    resumable: true,
    resumable_detail: null,
    ...over,
  }
}

function listing(over: Partial<CheckpointsPage> = {}): CheckpointsPage {
  const checkpoints = over.checkpoints ?? [
    record('ckpt-00002', { is_latest_pointer: true }),
    // The row that must not read as "cannot be resumed": the manifest is
    // there and did not parse, so nothing inside it is known.
    record('ckpt-00001', {
      attempt_known: false,
      manifest: 'unreadable',
      manifest_detail: 'the object is there and did not parse as JSON',
      created_at: null,
      seq: null,
      file_count: null,
      resumable: null,
      resumable_detail: 'nothing could be established without the manifest',
      objects: [{ name: 'workspace.tar.zst', key: `${PREFIX}/ckpt-00001/workspace.tar.zst`, bytes: 9_102_336 }],
      stored_bytes: 9_102_336,
    }),
  ]
  return {
    task_id: 'tsk_files',
    tenant_id: 'acme',
    prefix: PREFIX,
    checkpoints,
    count: checkpoints.length,
    total_found: checkpoints.length,
    next_page_token: null,
    listed: true,
    truncated: false,
    latest_checkpoint: { pointer: `${PREFIX}/ckpt-00002`, status: 'present', checkpoint_id: 'ckpt-00002' },
    ...over,
  }
}

function stream(name: 'stdout' | 'stderr', over: Partial<LogStream> = {}): LogStream {
  return {
    stream: name,
    source: 'final',
    status: 'ok',
    detail: null,
    key: `tenants/acme/tasks/tsk_files/logs/att_1.${name}`,
    uri: `gs://swarm-logs/tenants/acme/tasks/tsk_files/logs/att_1.${name}`,
    content: '[13:42:01] cloning\n[13:42:09] ready\n',
    total_bytes: 4096,
    offset: 0,
    returned_bytes: 4096,
    next_offset: null,
    truncated: false,
    redacted: false,
    redaction_count: 0,
    tail_window: null,
    ...over,
  }
}

function logs(streams: LogStream[], over: Partial<TaskLogs> = {}): TaskLogs {
  return {
    task_id: 'tsk_files',
    tenant_id: 'acme',
    attempt_id: 'att_1',
    attempt: {
      status: 'latest',
      known: true,
      generation: 1,
      created_at: iso(30),
      completed_at: iso(3),
      exit_code: 0,
    },
    streams,
    prefix: 'tenants/acme/tasks/tsk_files/logs',
    redaction: { applied_at_read_time: true, rules: 12 },
    ...over,
  }
}

const TASK: Task = task({ id: 'tsk_files', state: 'SUCCEEDED' })

async function panel(
  title: 'Checkpoints' | 'Output, as the agent wrote it',
  reads: { checkpoints?: Result<CheckpointsPage>; logs?: Result<TaskLogs> },
  attempts = [attempt(1)],
  t: Task = TASK,
): Promise<HTMLElement> {
  api.loadCheckpoints.mockResolvedValue(reads.checkpoints ?? { status: 'empty', fetchedAt: NOW })
  api.loadTaskLogs.mockResolvedValue(reads.logs ?? { status: 'empty', fetchedAt: NOW })
  const { container } = render(<RunFiles task={t} attempts={attempts} />)
  return waitFor(
    () => {
      const s = [...container.querySelectorAll<HTMLElement>('section')].find(
        (x) => x.querySelector('h2')?.textContent === title,
      )
      expect(s, `no ${title} section`).toBeTruthy()
      expect(s!.textContent ?? '').not.toMatch(/Reading…/)
      return s!
    },
    { timeout: 5000 },
  )
}

/** Every element in a panel that draws a box of its own (§13.3's panel level). */
function boxes(section: Element): Element[] {
  return [...section.querySelectorAll('.ctl-table, .ctl-card, .ckpt, .ckpt-rows, .logwin, .ckb, dl.kv')]
}

/** The body row whose row header starts with `name`. */
function rowFor(section: Element, name: string): HTMLTableRowElement {
  const row = [...section.querySelectorAll<HTMLTableRowElement>('tbody tr')].find((tr) =>
    (tr.querySelector('th')?.textContent ?? '').trim().startsWith(name),
  )
  expect(row, `no table row for ${name}`).toBeTruthy()
  return row!
}

function cell(row: HTMLTableRowElement, label: string): HTMLElement {
  const c = row.querySelector<HTMLElement>(`td[data-label="${label}"]`)
  expect(c, `the row has no ${label} cell`).not.toBeNull()
  return c!
}

function heads(section: Element): string[] {
  return [...section.querySelectorAll('thead th')].map((th) => (th.textContent ?? '').trim())
}

/** The strip's `restore` fact: what a retry of this task would restore from. */
function restoreFact(section: Element): HTMLElement {
  const li = [...section.querySelectorAll<HTMLElement>('.ctl-facts .ctl-fact')].find(
    (f) => (f.querySelector('b')?.textContent ?? '') === 'restore',
  )
  expect(li, 'the strip has no restore fact').toBeTruthy()
  return li!
}

/**
 * Three checkpoints a retry could restore from, newest first by the worker's
 * own order (the manifest's `created_at`, then `seq`), none refused and none
 * unread -- so the newest is the answer and nothing qualifies it.
 */
function resumableRows(): CheckpointRecord[] {
  return [
    record('ckpt-00003', { attempt_id: 'att_2', created_at: iso(2), seq: 3 }),
    record('ckpt-00002', { created_at: iso(6), seq: 2 }),
    record('ckpt-00001', { created_at: iso(9), seq: 1 }),
  ]
}

// ---------------------------------------------------------------------------
// Checkpoints
// ---------------------------------------------------------------------------

describe('the Checkpoints panel is one table of checkpoints', () => {
  it('draws one box: a table with name, size and age, and no card or dl.kv per checkpoint', async () => {
    const s = await panel('Checkpoints', { checkpoints: ok(listing()) })
    expect(boxes(s).map((b) => b.className)).toEqual(['ctl-table is-stacked'])
    // THREE COLUMNS, as decided: name, size, age -- and nothing after them.
    expect(heads(s)).toEqual(['Checkpoint', 'Size', 'Age'])
    expect(s.querySelectorAll('tbody tr')).toHaveLength(2)
    // Each checkpoint still opens its own file browser.
    expect([...s.querySelectorAll('button')].filter((b) => b.textContent === 'files')).toHaveLength(2)
    // The prefix keeps the hook the sheet breaks anywhere (AG-26).
    expect(s.querySelector('code.ckpt-prefix')?.textContent).toBe(PREFIX)
  })

  it('states a readable checkpoint as figures: its bytes and its age', async () => {
    const s = await panel('Checkpoints', { checkpoints: ok(listing()) })
    const row = rowFor(s, 'ckpt-00002')
    expect(cell(row, 'Size').textContent).toMatch(/MiB/)
    expect(cell(row, 'Age').textContent).toMatch(/\d+m ago|just now/)
    expect(row.querySelector('.ctl-mark'), 'a checkpoint that read cleanly carries a mark').toBeNull()
  })

  it('marks what the unreadable manifest took away, and never calls it a zero or a no', async () => {
    const s = await panel('Checkpoints', { checkpoints: ok(listing()) })
    const row = rowFor(s, 'ckpt-00001')
    // NO AGE: the manifest carries the time and it did not parse.
    const age = cell(row, 'Age')
    expect(age.querySelector('.ctl-em')).not.toBeNull()
    expect(age.querySelector('.ctl-mark')).not.toBeNull()
    expect(age.textContent).not.toMatch(/\d/)
    // RESUMABLE IS NULL, which is "cannot tell", never "no". It is a line
    // under the checkpoint's name now, not a fourth column.
    const resume = row.querySelector<HTMLElement>('th [data-testid="rf-resume"]')
    expect(resume, 'whether a retry would restore from this is not said under its name').not.toBeNull()
    const mark = resume!.querySelector('.ctl-mark.is-unread')
    expect(mark, '"cannot tell" is not marked as a read that failed').not.toBeNull()
    expect(mark!.getAttribute('aria-label') ?? '').toMatch(/cannot tell/i)
    expect(resume!.textContent).not.toMatch(/\bno\b/)
    // And a checkpoint that read cleanly says yes, under its own name.
    expect(rowFor(s, 'ckpt-00002').querySelector('th [data-testid="rf-resume"]')?.textContent).toMatch(/resume\s*yes/)
  })

  it('draws a checkpoint\'s objects as rows of the same table, each with a name, a size and an age', async () => {
    const s = await panel('Checkpoints', { checkpoints: ok(listing()) })
    const toggle = [...s.querySelectorAll('button')].find((b) => b.textContent === '2 objects')
    expect(toggle, 'no control opens ckpt-00002\'s objects').toBeTruthy()
    expect(toggle!.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle!)
    await waitFor(() => expect(s.querySelectorAll('tbody tr.rf-object')).toHaveLength(2))
    expect(toggle!.getAttribute('aria-expanded')).toBe('true')
    // STILL ONE BOX: the objects are rows of the checkpoint table, not a
    // second table or a card inside it.
    expect(boxes(s).map((b) => b.className)).toEqual(['ctl-table is-stacked'])
    const rows = [...s.querySelectorAll<HTMLTableRowElement>('tbody tr')]
    const at = rows.findIndex((r) => (r.querySelector('th')?.textContent ?? '').trim().startsWith('ckpt-00002'))
    // Directly under the checkpoint they belong to, in the listing's order.
    expect(rows[at + 1]!.classList.contains('rf-object')).toBe(true)
    expect(rows[at + 2]!.classList.contains('rf-object')).toBe(true)
    const archive = rows[at + 2]!
    expect(archive.querySelector('th')?.textContent).toContain('workspace.tar.zst')
    expect(cell(archive, 'Size').textContent).toMatch(/MiB/)
    // THE LISTING SERVES NO TIME FOR AN OBJECT, so its age is the em dash and
    // the `not measured` mark -- never the checkpoint's own age borrowed.
    const age = cell(archive, 'Age')
    expect(age.querySelector('.ctl-em')).not.toBeNull()
    expect(age.querySelector('.ctl-mark.is-absent')).not.toBeNull()
    expect(age.textContent).not.toMatch(/\d/)
    // Every row keeps the table's three cells.
    for (const r of rows) expect(r.querySelectorAll(':scope > th, :scope > td'), r.textContent ?? '').toHaveLength(3)
  })

  it('draws a whole, empty listing with nothing recorded as the real-zero mark, not a sentence', async () => {
    const s = await panel('Checkpoints', { checkpoints: ok(listing({ checkpoints: [], latest_checkpoint: { pointer: null, status: 'unset', checkpoint_id: null } })) })
    const empty = s.querySelector('.ctl-empty')
    expect(empty, 'the empty listing is not the shared empty state').not.toBeNull()
    expect(empty!.className).toBe('ctl-empty')
    expect(empty!.querySelector('.ctl-mark.is-zero')).not.toBeNull()
    expect(s.textContent, 'the real zero is still written out as prose').not.toMatch(/A real zero\./)
    expect(s.querySelector('.ctl-table'), 'an empty table was drawn beside the empty state').toBeNull()
  })

  it('draws a listing that did not complete as a failed read, and a cut one as partial', async () => {
    const failed = await panel('Checkpoints', { checkpoints: ok(listing({ checkpoints: [], listed: false })) })
    expect(failed.querySelector('.ctl-empty.is-failed .ctl-mark.is-unread')).not.toBeNull()
    const cut = await panel('Checkpoints', {
      checkpoints: ok(listing({ checkpoints: [], truncated: true, next_page_token: 'more' })),
    })
    expect(cut.querySelector('.ctl-empty.is-partial .ctl-mark.is-partial')).not.toBeNull()
  })

  it('joins the server\'s lowercase detail to the pointer sentence instead of after a full stop', async () => {
    const detail = 'the pointer names a checkpoint of this task that is no longer in the bucket; it may have been reclaimed'
    const s = await panel(
      'Checkpoints',
      {
        checkpoints: ok(
          listing({
            checkpoints: [],
            latest_checkpoint: {
              pointer: `gs://swarm-workspaces/${PREFIX}/ckpt-00009/`,
              status: 'missing',
              checkpoint_id: 'ckpt-00009',
              detail,
            },
          }),
        ),
      },
      [attempt(1, { checkpoints: ['ckpt-00009'] })],
    )
    const text = s.textContent ?? ''
    expect(text).toContain(detail)
    expect(text, 'a lowercase detail still follows a full stop').not.toMatch(/\.\s+the pointer names/)
  })
})

describe('the restore fact says what a retry would restore from', () => {
  const OUTSIDE = 'gs://swarm-workspaces/tenants/acme/tasks/tsk_other/attempts/att_7/checkpoints/ckpt-00004/'

  it('names the newest committed checkpoint when the pointer is outside this task, and says nothing restarts', async () => {
    const detail =
      "the pointer does not name a checkpoint under this task's own prefix, so a resuming worker would ignore it and fall back to the newest committed checkpoint it can find"
    const s = await panel('Checkpoints', {
      checkpoints: ok(
        listing({
          checkpoints: resumableRows(),
          latest_checkpoint: { pointer: OUTSIDE, status: 'outside_this_task', checkpoint_id: null, detail },
        }),
      ),
    })
    const fact = restoreFact(s)
    expect(fact.textContent).toMatch(/newest committed/)
    // The newest by the worker's order, named with its attempt: ids restart
    // per attempt, so `ckpt-00003` alone could be any attempt's.
    expect(fact.querySelector('code')?.textContent).toBe('att_2/ckpt-00003')
    expect(fact.querySelector('.ctl-mark'), 'a whole, readable listing needs no qualifier').toBeNull()
    // THE POINTER IS NOT THE RESTORE SOURCE, so it is not the fact's value.
    expect(fact.textContent).not.toContain('gs://')
    // And nothing on the panel says the task would start over.
    expect(s.textContent, 'the panel says a task with a resumable checkpoint restarts').not.toMatch(
      /restart from nothing|starts? from the beginning/i,
    )
    // The pointer is still said -- once, where it can wrap.
    const finding = s.querySelector('p.rollup')
    expect(finding?.querySelector('code')?.textContent).toBe(OUTSIDE)
    expect(finding?.textContent).toContain(detail)
    for (const m of s.querySelectorAll('span.mono')) {
      expect(m.textContent ?? '', 'a gs:// path is set in an element nothing lets wrap').not.toContain('gs://')
    }
  })

  it('names the newest committed checkpoint when no pointer is set, rather than a restart', async () => {
    const s = await panel('Checkpoints', {
      checkpoints: ok(
        listing({
          checkpoints: resumableRows(),
          latest_checkpoint: { pointer: null, status: 'unset', checkpoint_id: null },
        }),
      ),
    })
    const fact = restoreFact(s)
    expect(fact.textContent).toMatch(/newest committed/)
    expect(fact.querySelector('code')?.textContent).toBe('att_2/ckpt-00003')
    expect(fact.textContent).not.toMatch(/beginning|unset/)
  })

  it('names the newest committed checkpoint when the pointer names one that is gone', async () => {
    const s = await panel('Checkpoints', {
      checkpoints: ok(
        listing({
          checkpoints: resumableRows(),
          latest_checkpoint: {
            pointer: `gs://swarm-workspaces/${PREFIX}/ckpt-00009/`,
            status: 'missing',
            checkpoint_id: 'ckpt-00009',
            detail: 'the pointer names a checkpoint of this task that is no longer in the bucket; it may have been reclaimed',
          },
        }),
      ),
    })
    const fact = restoreFact(s)
    expect(fact.querySelector('code')?.textContent).toBe('att_2/ckpt-00003')
    expect(fact.textContent).not.toContain('ckpt-00009')
  })

  it('names the pointer\'s own checkpoint when the listing holds it', async () => {
    const s = await panel('Checkpoints', { checkpoints: ok(listing()) })
    expect(restoreFact(s).querySelector('code')?.textContent).toBe('ckpt-00002')
  })

  it('says none is listed when no row is resumable, and never names a refused one', async () => {
    const s = await panel('Checkpoints', {
      checkpoints: ok(
        listing({
          checkpoints: [
            record('ckpt-00001', {
              resumable: false,
              resumable_detail: 'the manifest names acme/tsk_other, not this task; a worker would refuse to restore it',
            }),
          ],
          latest_checkpoint: { pointer: null, status: 'unset', checkpoint_id: null },
        }),
      ),
    })
    const fact = restoreFact(s)
    expect(fact.textContent).toMatch(/none listed/)
    expect(fact.querySelector('code')).toBeNull()
  })

  it('qualifies the answer where the listing cannot vouch for it', async () => {
    // CUT: a newer checkpoint may be past the cut.
    const cut = await panel('Checkpoints', {
      checkpoints: ok(
        listing({
          checkpoints: resumableRows(),
          next_page_token: 'more',
          latest_checkpoint: { pointer: null, status: 'unset', checkpoint_id: null },
        }),
      ),
    })
    expect(restoreFact(cut).querySelector('.ctl-mark.is-partial'), 'a cut listing is vouched for as whole').not.toBeNull()

    // UNREAD: a manifest nobody could read may be the newest a worker finds.
    const unread = await panel('Checkpoints', {
      checkpoints: ok(
        listing({
          checkpoints: [
            ...resumableRows(),
            record('ckpt-00004', { manifest: 'unreadable', created_at: null, seq: null, resumable: null }),
          ],
          latest_checkpoint: { pointer: null, status: 'unset', checkpoint_id: null },
        }),
      ),
    })
    expect(restoreFact(unread).querySelector('.ctl-mark.is-unread'), 'an unread manifest is ignored').not.toBeNull()

    // NEWER AND NOT RESUMABLE: a worker takes the newest manifest it can read
    // and owns, so it may try that one first.
    const newer = await panel('Checkpoints', {
      checkpoints: ok(
        listing({
          checkpoints: [
            record('ckpt-00005', {
              attempt_id: 'att_3',
              created_at: iso(1),
              seq: 5,
              resumable: false,
              resumable_detail: 'the archive named by the manifest is not in the bucket',
            }),
            ...resumableRows(),
          ],
          latest_checkpoint: { pointer: null, status: 'unset', checkpoint_id: null },
        }),
      ),
    })
    const fact = restoreFact(newer)
    expect(fact.querySelector('code')?.textContent).toBe('att_2/ckpt-00003')
    const mark = fact.querySelector('.ctl-mark.is-partial')
    expect(mark, 'a newer, unresumable checkpoint is not mentioned').not.toBeNull()
    expect(mark!.getAttribute('aria-label') ?? '').toContain('att_3/ckpt-00005')
  })
})

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

describe('the log panel is one table of streams', () => {
  it('draws one box: a table with name, size and age, and no card per stream', async () => {
    const s = await panel('Output, as the agent wrote it', {
      logs: ok(logs([stream('stdout'), stream('stderr', { content: '', total_bytes: 0, returned_bytes: 0 })])),
    })
    expect(boxes(s).map((b) => b.className)).toEqual(['ctl-table is-stacked'])
    expect(heads(s).slice(0, 3)).toEqual(['Stream', 'Size', 'Age'])
    expect(s.querySelectorAll('tbody tr')).toHaveLength(2)
    // The window itself is still there, as the agent wrote it.
    expect(s.querySelector('pre.logwin-body')?.textContent).toContain('[13:42:09] ready')
  })

  it('draws a measured empty stream as the real-zero mark, and the paragraph is gone', async () => {
    const s = await panel('Output, as the agent wrote it', {
      logs: ok(logs([stream('stdout'), stream('stderr', { content: '', total_bytes: 0, returned_bytes: 0 })])),
    })
    const size = cell(rowFor(s, 'stderr'), 'Size')
    expect(size.querySelector('.ctl-mark.is-zero'), 'an empty stream is not marked as a real zero').not.toBeNull()
    expect(s.textContent).not.toMatch(/This object exists and is empty/)
    expect(s.textContent).not.toMatch(/real, measured empty stream/)
  })

  it('marks a truncated window partial, a missing object absent and an unreadable one not read', async () => {
    const s = await panel('Output, as the agent wrote it', {
      logs: ok(
        logs([
          stream('stdout', { truncated: true, returned_bytes: 1024, next_offset: 1024 }),
          stream('stderr', {
            status: 'unreadable',
            content: null,
            total_bytes: null,
            returned_bytes: 0,
            detail: 'permission denied reading the object',
          }),
        ]),
      ),
    })
    expect(cell(rowFor(s, 'stdout'), 'Size').querySelector('.ctl-mark.is-partial')).not.toBeNull()
    const unreadable = cell(rowFor(s, 'stderr'), 'Size')
    expect(unreadable.querySelector('.ctl-mark.is-unread')).not.toBeNull()
    expect(unreadable.querySelector('.ctl-em')).not.toBeNull()
    expect(s.textContent).toContain('permission denied reading the object')
    expect(s.textContent, 'a lowercase detail still follows a full stop').not.toMatch(/\.\s+permission denied/)

    const absent = await panel('Output, as the agent wrote it', {
      logs: ok(logs([stream('stdout', { status: 'absent', source: null, content: null, total_bytes: null, returned_bytes: 0 })])),
    })
    const size = cell(rowFor(absent, 'stdout'), 'Size')
    expect(size.querySelector('.ctl-mark.is-absent')).not.toBeNull()
    expect(size.textContent).not.toMatch(/\d/)
  })

  it('says a task with no attempt has written no log, once, as a real zero', async () => {
    // THE ROUTE'S OWN SHAPE (`_no_attempt_entry`, swarm_api/inspect.py): with
    // no attempt it still names both streams, each absent and each carrying
    // the same detail. An empty `streams` array -- what this case used to
    // hand the panel -- is not what the server sends, and it hid the fact
    // being said three times.
    const none = 'this task has no attempt yet, so no log object can exist'
    const absentStream = (name: 'stdout' | 'stderr') =>
      stream(name, {
        source: null,
        status: 'absent',
        detail: none,
        key: null,
        uri: null,
        content: null,
        total_bytes: null,
        returned_bytes: 0,
      })
    const s = await panel('Output, as the agent wrote it', {
      logs: ok(
        logs([absentStream('stdout'), absentStream('stderr')], {
          attempt_id: null,
          attempt: { status: 'no_attempt_yet', known: false, generation: null, created_at: null, completed_at: null, exit_code: null },
        }),
      ),
    })
    expect(s.textContent).toMatch(/no attempt yet/i)
    expect(s.querySelector('.ctl-empty .ctl-mark.is-zero')).not.toBeNull()
    expect(s.querySelector('.ctl-table'), 'a table of two rows that each say there is no attempt').toBeNull()
    expect(s.textContent, 'the per-stream detail restates the empty state').not.toContain(none)
    expect((s.textContent ?? '').match(/attempt yet/gi) ?? [], 'the fact is said more than once').toHaveLength(1)
  })

  it('calls a tail live only while its attempt is still being written', async () => {
    const tail = stream('stdout', { source: 'live', tail_window: null })
    const open = { status: 'latest' as const, known: true, generation: 1, created_at: iso(30), completed_at: null, exit_code: null }
    const ended = { ...open, completed_at: iso(3), exit_code: 137 }

    // RUNNING, attempt open: the tail is being written.
    const running = await panel(
      'Output, as the agent wrote it',
      { logs: ok(logs([tail], { attempt: open })) },
      [attempt(1)],
      task({ id: 'tsk_files', state: 'RUNNING' }),
    )
    expect(cell(rowFor(running, 'stdout'), 'Age').textContent).toBe('live')

    // FAILED, attempt ended: the worker never reached its upload, and what is
    // left is the last tail it published. Nothing is writing it.
    const cases: [Task, TaskLogs['attempt']][] = [
      [task({ id: 'tsk_files', state: 'FAILED' }), ended],
      // PARKED on quota: `park()` never writes the attempt's end, so
      // `completed_at` stays null for ever -- and still nothing is writing.
      [task({ id: 'tsk_files', state: 'PARKED', park_reason: 'PROVIDER_QUOTA_EXHAUSTED' }), open],
    ]
    for (const [t, a] of cases) {
      const s = await panel('Output, as the agent wrote it', { logs: ok(logs([tail], { attempt: a })) }, [attempt(1)], t)
      const age = cell(rowFor(s, 'stdout'), 'Age')
      expect(age.textContent ?? '', `a ${t.state} task's leftover tail is called live`).not.toMatch(/\blive\b/)
      expect(age.querySelector('.ctl-em')).not.toBeNull()
      const mark = age.querySelector('.ctl-mark.is-partial')
      expect(mark, `a ${t.state} task's leftover tail is not marked partial`).not.toBeNull()
      expect(mark!.getAttribute('aria-label') ?? '').toMatch(/no final log was uploaded/i)
    }
  })

  it('marks a final log whose attempt end is not recorded, rather than a bare dash', async () => {
    const s = await panel('Output, as the agent wrote it', {
      logs: ok(
        logs([stream('stdout')], {
          attempt: { status: 'latest', known: true, generation: 1, created_at: iso(30), completed_at: null, exit_code: null },
        }),
      ),
    })
    const age = cell(rowFor(s, 'stdout'), 'Age')
    expect(age.querySelector('.ctl-em')).not.toBeNull()
    expect(age.querySelector('.ctl-mark.is-absent'), 'an unknown age is drawn as a bare dash').not.toBeNull()
  })
})
