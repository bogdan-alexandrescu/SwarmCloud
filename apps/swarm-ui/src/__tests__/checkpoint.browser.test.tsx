// THE CHECKPOINT BROWSER, AS BEHAVIOUR. Owner decision 2026-09-24 (A3).
//
// The server keeps four answers apart -- a listing, an absent archive, a
// corrupt one and a failed read -- and a screen can collapse all four into
// "no files" with one `?? []`. These tests mount the real component with the
// loaders it is given and assert on the DOM, because every one of the
// distinctions below is a string that ALSO appears in the component's own
// comments, which is exactly why a source grep could not test them.
//
//   * `files: null` (absent) is drawn as a missing archive, never as "0 files";
//   * `files: []` is a measured zero ONLY when the listing was complete;
//   * a cut listing carries a `partial` mark and names the cut;
//   * a failed read is `not read`, with no figure and a retry;
//   * an unsafe member is listed and cannot be opened;
//   * a name that is not UTF-8 is listed escaped, not openable, and not unsafe;
//   * a file opens in the SAME ArtifactViewer the run's outputs use, fed by
//     the checkpoint per-file read;
//   * the loaders build the paths the router serves, and never a `..`.

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import {
  CheckpointBrowser,
  buildTree,
  checkpointDownloadHref,
  loadCheckpointFile,
  loadCheckpointFiles,
  memberHref,
  modeLabel,
  type CheckpointFileContent,
  type CheckpointFiles,
  type CheckpointLoaders,
  type CheckpointMember,
} from '../CheckpointBrowser'
import { RunFiles } from '../RunFiles'
import type { Result } from '../fetch'
import type { Task } from '../types'
import { expectNoFigures } from './setup'

const PREFIX = 'tenants/eng/tasks/task_a/attempts/att_1/checkpoints/ckpt-00001'

function member(path: string, over: Partial<CheckpointMember> = {}): CheckpointMember {
  return { path, size: 10, mode: 0o644, type: 'file', link: null, unsafe: false, undecodable: false, ...over }
}

const MEMBERS: CheckpointMember[] = [
  member('progress', { type: 'dir', size: 0, mode: 0o755 }),
  member('progress/step-0001.md', { size: 32 }),
  member('state.json', { size: 23 }),
  member('bin/run.sh', { mode: 0o755 }),
  member('latest', { type: 'symlink', size: 0, mode: 0o777, link: 'state.json' }),
]

function listing(over: Partial<CheckpointFiles> = {}): CheckpointFiles {
  return {
    task_id: 'task_a',
    tenant_id: 'eng',
    attempt_id: 'att_1',
    checkpoint_id: 'ckpt-00001',
    prefix: PREFIX,
    archive: { key: `${PREFIX}/archive.tar.gz`, uri: `gs://bucket/${PREFIX}/archive.tar.gz`, bytes: 4096 },
    manifest: {
      status: 'present',
      detail: null,
      file_count: 4,
      archive_bytes: 4096,
      archive_sha256: 'abc',
      created_at: null,
      label: 'periodic',
      archive_key_agrees: true,
    },
    status: 'ok',
    detail: null,
    files: MEMBERS,
    count: MEMBERS.length,
    truncated: false,
    truncated_reason: null,
    file_count_agrees: true,
    scanned_bytes: 4096,
    inflated_bytes: 20480,
    limits: { entries: 5000, scan_bytes: 268435456, inflate_bytes: 1073741824 },
    ...over,
  }
}

function content(path: string, text: string | null, over: Partial<CheckpointFileContent> = {}): CheckpointFileContent {
  return {
    task_id: 'task_a',
    tenant_id: 'eng',
    attempt_id: 'att_1',
    checkpoint_id: 'ckpt-00001',
    path,
    content_type: 'text/plain',
    member: { type: 'file', mode: 0o644, size: text?.length ?? 0 },
    artifact: { name: path, bytes: text?.length ?? 0, uri: `gs://bucket/${PREFIX}/archive.tar.gz` },
    status: 'ok',
    detail: null,
    key: `${PREFIX}/archive.tar.gz`,
    uri: `gs://bucket/${PREFIX}/archive.tar.gz`,
    content: text,
    total_bytes: text?.length ?? 0,
    offset: 0,
    returned_bytes: text?.length ?? 0,
    next_offset: null,
    truncated: false,
    redacted: false,
    redaction_count: 0,
    redaction: { applied_at_read_time: true, rules: 11 },
    ...over,
  }
}

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function loaders(
  files: Result<CheckpointFiles>,
  file: (path: string) => Result<CheckpointFileContent> = (p) => ok(content(p, `contents of ${p}\n`)),
) {
  const spy = {
    files: vi.fn(async () => files),
    file: vi.fn(async (_t: string, _a: string, _c: string, p: string) => file(p)),
  }
  return spy as typeof spy & CheckpointLoaders
}

function mount(l: CheckpointLoaders) {
  return render(
    <CheckpointBrowser
      taskId="task_a"
      attemptId="att_1"
      checkpointId="ckpt-00001"
      onClose={() => {}}
      loaders={l}
    />,
  )
}

const region = () => screen.getByRole('region', { name: 'Checkpoint ckpt-00001 files' })

// ---------------------------------------------------------------------------
// The listing
// ---------------------------------------------------------------------------

describe('a listing', () => {
  it('draws the archive as a tree, directories first, with sizes, modes and link targets', async () => {
    const l = loaders(ok(listing()))
    mount(l)

    const tree = await screen.findByRole('tree', { name: 'Files in this checkpoint' })
    expect(l.files).toHaveBeenCalledWith('task_a', 'att_1', 'ckpt-00001')

    const top = within(tree).getAllByRole('treeitem').filter((li) => li.parentElement === tree)
    expect(top.map((li) => li.querySelector('.ckb-open, .ckb-name')?.textContent)).toEqual([
      // `bin` is IMPLIED by `bin/run.sh` -- the archive has no entry for it --
      // and is still a directory in the tree.
      '▾ bin/',
      '▾ progress/',
      'latest → state.json',
      'state.json',
    ])
    expect(within(tree).getByRole('button', { name: 'step-0001.md' })).toBeTruthy()
    expect(within(tree).getByRole('button', { name: 'run.sh' })).toBeTruthy()
    // A symlink is NOT an openable file: its target is on the surface instead.
    expect(within(tree).queryByRole('button', { name: /latest/ })).toBeNull()
    expect(tree.textContent).toContain('0755')
    expect(tree.textContent).toContain('symlink')
  })

  it('opens a file in the artifact viewer, fed by the checkpoint per-file read', async () => {
    const l = loaders(ok(listing()), (p) => ok(content(p, '{"completed_steps": 1}\n')))
    mount(l)

    fireEvent.click(await screen.findByRole('button', { name: 'state.json' }))

    const viewer = await screen.findByRole('region', { name: 'Artifact state.json' })
    expect(await within(viewer).findByText('{"completed_steps": 1}')).toBeTruthy()
    expect(l.file).toHaveBeenCalledTimes(1)
    expect(l.file).toHaveBeenCalledWith('task_a', 'att_1', 'ckpt-00001', 'state.json')
  })

  // #207: the member read counts the bytes it showed as U+FFFD, as the
  // Artifacts tab's read does, and the viewer draws the count here too. Its
  // mark must name the download that HOLDS those bytes: this route has no
  // per-member raw download, only the whole-checkpoint archive.
  const notUtf8 = (viewer: HTMLElement) =>
    [...viewer.querySelectorAll('li.ctl-fact')].find((li) => li.querySelector('b')?.textContent === 'not utf-8')

  it('draws a member\'s not-utf-8 count, and names the checkpoint download as what holds the bytes (#207)', async () => {
    const l = loaders(ok(listing()), (p) =>
      ok(content(p, 'caf\uFFFD au lait \uFFFD\n', { invalid_utf8_bytes: 2 })),
    )
    mount(l)
    fireEvent.click(await screen.findByRole('button', { name: 'state.json' }))
    const viewer = await screen.findByRole('region', { name: 'Artifact state.json' })
    await waitFor(() => expect(notUtf8(viewer), 'bytes shown as U+FFFD are not counted').toBeTruthy())
    const fact = notUtf8(viewer)!
    expect(fact.textContent).toContain('not utf-8')
    // The <b> label, then the count: `not utf-8` `2` `partial`.
    expect(fact.textContent!.replace('not utf-8', '').trim()).toMatch(/^2\b/)
    const say = fact.querySelector('.ctl-mark.is-partial')?.getAttribute('aria-label') ?? ''
    expect(say).toMatch(/checkpoint download/)
    expect(say, 'there is no per-member raw download on this route').not.toMatch(/raw download/)
  })

  it('draws no not-utf-8 fact for a member whose window measured zero', async () => {
    const l = loaders(ok(listing()), (p) => ok(content(p, '{"completed_steps": 1}\n', { invalid_utf8_bytes: 0 })))
    mount(l)
    fireEvent.click(await screen.findByRole('button', { name: 'state.json' }))
    const viewer = await screen.findByRole('region', { name: 'Artifact state.json' })
    await within(viewer).findByText('{"completed_steps": 1}')
    expect(notUtf8(viewer), 'a zero count is drawn as a finding').toBeUndefined()
  })

  it('reads a file ONCE, however often the browser re-renders', async () => {
    // ArtifactViewer re-reads when its loader changes identity; a fresh
    // closure per render would be a fresh request per render.
    const l = loaders(ok(listing()))
    mount(l)
    fireEvent.click(await screen.findByRole('button', { name: 'state.json' }))
    await screen.findByRole('region', { name: 'Artifact state.json' })
    // Toggling a directory re-renders the listing around the open viewer.
    fireEvent.click(screen.getByRole('button', { name: '▾ progress/' }))
    fireEvent.click(screen.getByRole('button', { name: '▸ progress/' }))
    await waitFor(() => expect(l.file).toHaveBeenCalledTimes(1))
  })

  it('shows a member the NUL sniff found binary as not text, with no bytes', async () => {
    // RE-POINTED. This used `shot.png` with `content_type: null`, which is not
    // the sniff case: a null content type is the server's allowlist REFUSING
    // the name before a byte was read (`checkpoint_content.content_type_for`),
    // so "not text" was a claim nobody had measured. That case now has its own
    // test below. What reaches "not text" is a name the allowlist accepted
    // whose first bytes held a NUL -- a finding, and the only one here.
    const l = loaders(ok(listing({ files: [member('data.log', { size: 48211 })], count: 1 })), (p) =>
      ok(content(p, null, { status: 'binary', content_type: 'text/plain', total_bytes: 48211, returned_bytes: 0 })),
    )
    mount(l)
    fireEvent.click(await screen.findByRole('button', { name: 'data.log' }))
    const viewer = await screen.findByRole('region', { name: 'Artifact data.log' })
    // RE-POINTED AGAIN (AG-29). The finding was drawn as `not text` beside the
    // `not measured` mark -- an absence mark next to a size that WAS measured.
    // It is the size and the word now, with no mark: nothing is missing, the
    // bytes are simply not text.
    expect(await within(viewer).findByText('binary · 47 KiB')).toBeTruthy()
    expect(viewer.querySelector('.ctl-mark'), 'a measured binary member carries an absence mark').toBeNull()
    expect(viewer.textContent).not.toContain('not measured')
    expect(viewer.querySelector('pre')).toBeNull()
  })

  it('offers the whole archive as a download link to the content route', async () => {
    mount(loaders(ok(listing())))
    await screen.findByRole('tree')
    const link = within(region()).getByRole('link', { name: 'download archive' })
    expect(link.getAttribute('href')).toBe(
      '/v1/tasks/task_a/checkpoints/ckpt-00001/content?attempt_id=att_1',
    )
    expect(link.hasAttribute('download')).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// The archive's own time (#172)
// ---------------------------------------------------------------------------

describe("the archive fact dates the archive by the bucket's own time (#172)", () => {
  const archiveFact = () =>
    [...region().querySelectorAll<HTMLElement>('.ctl-facts .ctl-fact')].find(
      (f) => (f.querySelector('b')?.textContent ?? '') === 'archive',
    )!

  it('states how long ago the archive object was written, beside its size', async () => {
    const at = new Date(Date.now() - 9 * 60_000).toISOString()
    mount(loaders(ok(listing({ archive: { key: `${PREFIX}/archive.tar.gz`, uri: `gs://bucket/${PREFIX}/archive.tar.gz`, bytes: 4096, object_updated_at: at } }))))
    await screen.findByRole('tree')
    expect(archiveFact().textContent).toMatch(/4(\.0)? KiB/)
    expect(archiveFact().textContent).toContain('9m ago')
  })

  it('states no time, and invents none, when the store reported none', async () => {
    mount(loaders(ok(listing({ archive: { key: `${PREFIX}/archive.tar.gz`, uri: `gs://bucket/${PREFIX}/archive.tar.gz`, bytes: 4096, object_updated_at: null } }))))
    await screen.findByRole('tree')
    expect(archiveFact().textContent).not.toMatch(/ago|just now/)
  })
})

// ---------------------------------------------------------------------------
// The four answers
// ---------------------------------------------------------------------------

describe('absence and failure are not an empty checkpoint', () => {
  it('draws an ABSENT archive as missing, never as zero files', async () => {
    mount(loaders(ok(listing({ status: 'absent', files: null, count: 0, detail: 'no archive object' }))))

    expect(await screen.findByText(/archive not in the bucket/)).toBeTruthy()
    expect(within(region()).getByText('not measured')).toBeTruthy()
    expect(screen.queryByRole('tree')).toBeNull()
    expect(region().textContent).not.toMatch(/0 files/)
    expect(region().querySelector('.ctl-mark.is-zero')).toBeNull()
    // Nothing to download: the server said so.
    expect(within(region()).queryByRole('link', { name: 'download archive' })).toBeNull()
  })

  it('draws a complete, empty archive as a measured zero', async () => {
    mount(loaders(ok(listing({ files: [], count: 0, file_count_agrees: true }))))

    expect(await screen.findByText(/^\s*0 files$/)).toBeTruthy()
    expect(region().querySelector('.ctl-mark.is-zero')?.textContent).toBe('none')
    expect(screen.queryByRole('tree')).toBeNull()
  })

  it('never calls an empty but CUT listing a zero', async () => {
    mount(
      loaders(
        ok(listing({ files: [], count: 0, truncated: true, truncated_reason: 'scan_budget', file_count_agrees: null })),
      ),
    )
    expect(await within(region()).findByText('partial')).toBeTruthy()
    expect(within(region()).getByText('read budget')).toBeTruthy()
    expect(region().textContent).not.toMatch(/0 files/)
    expect(region().querySelector('.ctl-mark.is-zero')).toBeNull()
  })

  it('draws a FAILED read as not read, with no figure, and retries it', async () => {
    const l = loaders({
      status: 'error',
      error: {
        kind: 'upstream_degraded',
        httpStatus: 503,
        code: 'upstream_unavailable',
        message: 'The checkpoint archive could not be read.',
      },
    })
    mount(l)

    const failed = (await screen.findByText('not read')).closest('.ctl-empty') as HTMLElement
    expect(failed).toBeTruthy()
    expect(failed.textContent).toContain('The checkpoint archive could not be read.')
    expectNoFigures(failed)
    expect(screen.queryByRole('tree')).toBeNull()
    expect(region().textContent).not.toMatch(/0 files/)

    fireEvent.click(within(failed).getByRole('button', { name: 'try again' }))
    await waitFor(() => expect(l.files).toHaveBeenCalledTimes(2))
  })

  // AG-10. A 404 IS AN ABSENCE, NOT A FAILURE. The listing route answers
  // `not_found` for a checkpoint whose object is gone -- reclaimed, or never
  // uploaded -- and the browser drew it as `not read` with a `try again` that
  // can only get the same 404, under a `download archive` link to an object
  // that does not exist.
  //
  // BREAK IT: drop the `not_found` case from `Body`, or from `absent`.
  it('draws a 404 as an absent archive, with no retry and no download', async () => {
    const l = loaders({
      status: 'error',
      error: {
        kind: 'not_found',
        httpStatus: 404,
        code: 'not_found',
        message: 'checkpoint ckpt-00001 not found for attempt att_1',
      },
    })
    mount(l)

    expect(await screen.findByText(/archive not in the bucket/)).toBeTruthy()
    expect(within(region()).getByText('not measured')).toBeTruthy()
    expect(within(region()).queryByText('not read'), 'a 404 is drawn as a failed read').toBeNull()
    expect(within(region()).queryByRole('button', { name: 'try again' }), 'a 404 offers a retry').toBeNull()
    expect(
      within(region()).queryByRole('link', { name: 'download archive' }),
      'a 404 offers a download of an object that is not there',
    ).toBeNull()
    expect(region().textContent).not.toMatch(/0 files/)
    // The server's own sentence about this checkpoint is the one sentence kept.
    expect(region().textContent).toContain('checkpoint ckpt-00001 not found for attempt att_1')
    expect(l.files).toHaveBeenCalledTimes(1)
  })

  it('draws a CORRUPT archive as unreadable past a point, with the rows before it marked partial', async () => {
    mount(
      loaders(
        ok(
          listing({
            status: 'corrupt',
            files: [member('early.txt')],
            count: 1,
            truncated: true,
            truncated_reason: 'corrupt',
            file_count_agrees: null,
            detail: 'the archive ends before its compressed stream does',
          }),
        ),
      ),
    )
    expect(await screen.findByText(/archive not readable past here/)).toBeTruthy()
    expect(within(region()).getByText('archive corrupt')).toBeTruthy()
    expect(within(region()).getByText('partial')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'early.txt' })).toBeTruthy()
  })

  it('names the cut when the entry cap stops the listing', async () => {
    mount(loaders(ok(listing({ truncated: true, truncated_reason: 'entry_cap', file_count_agrees: null }))))
    expect(await within(region()).findByText('entry cap')).toBeTruthy()
    const mark = within(region()).getByText('partial')
    expect(mark.getAttribute('aria-label')).toMatch(/stops at 5000 entries/)
  })

  it('marks a listing that disagrees with its manifest', async () => {
    mount(loaders(ok(listing({ file_count_agrees: false }))))
    await screen.findByRole('tree')
    const mark = within(region()).getByText('partial')
    expect(mark.getAttribute('aria-label')).toMatch(/manifest records 4 files/)
  })

  it('lists an unsafe member apart from the tree and offers no way to open it', async () => {
    mount(
      loaders(
        ok(listing({ files: [...MEMBERS, member('../outside.txt', { unsafe: true })], count: 6 })),
      ),
    )
    expect(await screen.findByText(/1 unsafe path/)).toBeTruthy()
    expect(screen.getByText('../outside.txt')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /outside/ })).toBeNull()
    const tree = screen.getByRole('tree')
    expect(tree.textContent).not.toContain('outside')
  })

  it('shows a member whose name is not UTF-8 by its escaped bytes, and offers no way to open it', async () => {
    // The server serves the name's bytes escaped (`caf\xe9.txt`) and flags the
    // row `undecodable`. The escaped form is a display, not an address -- the
    // server refuses it as a path -- so the row is not a button. It is NOT
    // drawn as unsafe: a restore unpacks such a name without complaint, and
    // "cannot be resumed" would be a false statement about this checkpoint.
    const odd = member('caf\\xe9.txt', { undecodable: true })
    const l = loaders(ok(listing({ files: [...MEMBERS, odd], count: MEMBERS.length + 1 })))
    mount(l)

    const tree = await screen.findByRole('tree', { name: 'Files in this checkpoint' })
    expect(tree.textContent).toContain('caf\\xe9.txt')
    expect(within(tree).queryByRole('button', { name: /caf/ })).toBeNull()
    expect(tree.textContent).toMatch(/not UTF-8/)
    expect(region().textContent).not.toMatch(/unsafe path/)
    expect(l.file).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// The loaders
// ---------------------------------------------------------------------------

describe('the loaders', () => {
  function answering(body: unknown) {
    const calls: string[] = []
    vi.stubGlobal('fetch', async (input: RequestInfo | URL) => {
      calls.push(String(input))
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    })
    return calls
  }

  it('lists through the files route, scoped to one attempt', async () => {
    const calls = answering(listing())
    const res = await loadCheckpointFiles('task a', 'att_1', 'ckpt-00001', { fixtures: false })
    expect(calls).toEqual(['/v1/tasks/task%20a/checkpoints/ckpt-00001/files?attempt_id=att_1'])
    expect(res.status).toBe('ok')
  })

  it('does not call an empty listing `empty`: `files: []` is an answer', async () => {
    answering(listing({ files: [], count: 0 }))
    const res = await loadCheckpointFiles('task_a', 'att_1', 'ckpt-00001', { fixtures: false })
    expect(res.status).toBe('ok')
  })

  it('reads one member with its path encoded segment by segment', async () => {
    const calls = answering(content('progress/step 1.md', 'x'))
    await loadCheckpointFile('task_a', 'att_1', 'ckpt-00001', 'progress/step 1.md', { fixtures: false })
    expect(calls).toEqual([
      '/v1/tasks/task_a/checkpoints/ckpt-00001/files/progress/step%201.md?attempt_id=att_1',
    ])
  })

  it('never requests a path a browser would normalise into a different one', async () => {
    const calls = answering(content('x', 'x'))
    for (const hostile of ['../x', 'a/../b', 'a//b', './a', 'a/']) {
      const res = await loadCheckpointFile('task_a', 'att_1', 'ckpt-00001', hostile, { fixtures: false })
      expect(res.status, hostile).toBe('error')
      expect(memberHref(hostile), hostile).toBeNull()
    }
    expect(calls).toEqual([])
  })

  it('builds the download href from the content route', () => {
    expect(checkpointDownloadHref('task_a', 'att 1', 'ckpt-00001')).toBe(
      '/v1/tasks/task_a/checkpoints/ckpt-00001/content?attempt_id=att+1',
    )
  })
})

describe('helpers', () => {
  it('formats permission bits the way ls readers expect', () => {
    expect(modeLabel(0o644)).toBe('0644')
    expect(modeLabel(0o4755)).toBe('4755')
  })

  it('keeps unsafe members out of the tree', () => {
    const { root, unsafe } = buildTree([member('a/b.txt'), member('../c', { unsafe: true })])
    expect([...root.children.keys()]).toEqual(['a'])
    expect(unsafe.map((m) => m.path)).toEqual(['../c'])
  })
})

// ---------------------------------------------------------------------------
// Wired: opened from the checkpoint list
// ---------------------------------------------------------------------------

describe('opened from the checkpoint list', () => {
  it('each checkpoint row opens its own browser, and the fixture exercises the absent archive', async () => {
    // The development fixtures, end to end: RunFiles reads the checkpoint
    // list, and each row's `files` opens the browser on THAT row's attempt
    // and checkpoint. `ckpt_0001` in the fixture has no archive, which is the
    // state most easily drawn as "empty".
    render(<RunFiles task={{ id: 'task_fixture' } as unknown as Task} />)

    const buttons = await screen.findAllByRole('button', { name: 'files' })
    expect(buttons).toHaveLength(2)

    fireEvent.click(buttons[0]!)
    const first = await screen.findByRole('region', { name: 'Checkpoint ckpt_0002 files' })
    expect(await within(first).findByRole('tree')).toBeTruthy()
    expect(within(first).getByText('entry cap')).toBeTruthy()
    expect(buttons[0]!.getAttribute('aria-expanded')).toBe('true')

    fireEvent.click(buttons[1]!)
    const second = await screen.findByRole('region', { name: 'Checkpoint ckpt_0001 files' })
    expect(await within(second).findByText(/archive not in the bucket/)).toBeTruthy()
    expect(second.textContent).not.toMatch(/0 files/)

    fireEvent.click(within(first).getByRole('button', { name: 'Close checkpoint files' }))
    await waitFor(() =>
      expect(screen.queryByRole('region', { name: 'Checkpoint ckpt_0002 files' })).toBeNull(),
    )
  })
})

// ---------------------------------------------------------------------------
// The ROW is the control, the keyboard reaches it, and a refusal is a refusal
// ---------------------------------------------------------------------------
//
// THE DEFECT, measured on the live inspector on 2026-09-24
// (`13-checkpoint-browser-1440.png` -> `14-checkpoint-file-1440.png`): a click
// on the `result.json` row opened nothing. Only the NAME was a button; the rest
// of the row -- the size, the mode, the empty run to the right edge, which is
// most of what a pointer lands on -- was inert text, and a click there was
// silently dropped. The two screenshots are identical but for the clock.
//
// And two answers the per-file route gives were drawn as something else:
//
//   * the ALLOWLIST refusing a name (`status: binary`, `content_type: null`,
//     decided before a byte is read) was drawn `not measured` + "not text" --
//     an absence, and a claim about the bytes that nobody measured;
//   * a 413 (the member lies past this request's read budget) was drawn as
//     "The API failed on this request" with a `try again` that can only ever
//     get the same answer.

/** The `.ckb-node` row a tree item draws for itself, not for its children. */
function rowOf(tree: HTMLElement, name: string): HTMLElement {
  // The item's own row is its direct child; a directory's item also holds its
  // children's rows further down, which must not be mistaken for its own.
  const node = within(tree)
    .getAllByRole('treeitem')
    .map((li) => [...li.children].find((c) => c.classList.contains('ckb-node')) as HTMLElement | undefined)
    .find((n) => n !== undefined && (n.textContent ?? '').startsWith(name))
  if (!node) throw new Error(`no row for ${name}`)
  return node
}

describe('a file row opens the viewer', () => {
  const FLAT = [member('result.json', { size: 409 }), member('progress/step-0001.txt', { size: 39 })]

  /**
   * MUTATION: move the open handler back onto the name button alone. The click
   * below lands on the size text beside the name and opens nothing -- the
   * live defect.
   */
  it('opens the file a click anywhere on its row names, and reads exactly that path', async () => {
    const l = loaders(ok(listing({ files: FLAT, count: FLAT.length })), (p) =>
      ok(content(p, `{"path": "${p}"}\n`)),
    )
    mount(l)
    const tree = await screen.findByRole('tree', { name: 'Files in this checkpoint' })

    const row = rowOf(tree, 'result.json')
    const meta = row.querySelector<HTMLElement>('.ckb-meta')
    expect(meta, 'the row lost its size and mode').not.toBeNull()
    fireEvent.click(meta!)

    const viewer = await screen.findByRole('region', { name: 'Artifact result.json' })
    expect(await within(viewer).findByText('{"path": "result.json"}')).toBeTruthy()
    expect(l.file).toHaveBeenCalledTimes(1)
    expect(l.file).toHaveBeenCalledWith('task_a', 'att_1', 'ckpt-00001', 'result.json')
  })

  /**
   * A DIRECTORY ROW IS THE SAME KIND OF CONTROL. Its entry count is the part
   * of the row beside the name, and a click there was dropped the same way.
   * MUTATION: leave the toggle on the name button alone.
   */
  it('opens and closes a directory on a click anywhere on its row', async () => {
    mount(loaders(ok(listing({ files: FLAT, count: FLAT.length }))))
    const tree = await screen.findByRole('tree', { name: 'Files in this checkpoint' })
    const meta = rowOf(tree, '▾ progress/').querySelector<HTMLElement>('.ckb-meta')
    expect(meta, 'the directory row lost its entry count').not.toBeNull()
    fireEvent.click(meta!)
    expect(within(tree).getByRole('button', { name: '▸ progress/' })).toBeTruthy()
    expect(within(tree).queryByRole('button', { name: 'step-0001.txt' })).toBeNull()
  })

  it('reads a nested member by its full path, not by the name the row shows', async () => {
    const l = loaders(ok(listing({ files: FLAT, count: FLAT.length })), (p) =>
      ok(content(p, `step one of ${p}\n`)),
    )
    mount(l)
    const tree = await screen.findByRole('tree', { name: 'Files in this checkpoint' })
    fireEvent.click(rowOf(tree, 'step-0001.txt'))

    const viewer = await screen.findByRole('region', { name: 'Artifact progress/step-0001.txt' })
    expect(await within(viewer).findByText('step one of progress/step-0001.txt')).toBeTruthy()
    expect(l.file).toHaveBeenCalledWith('task_a', 'att_1', 'ckpt-00001', 'progress/step-0001.txt')
  })

  /**
   * THE KEYBOARD. Every openable row is a native button, so Tab reaches it and
   * Enter and Space open it -- the browser does that, and jsdom does not
   * synthesise it, so what is asserted here is the part this file adds: a
   * `role="tree"` is expected to move between its rows on the arrow keys and
   * to open and close a directory on Right and Left.
   *
   * MUTATION: drop the tree's key handler. Focus stays where it was.
   */
  it('moves between rows on the arrow keys, and opens and closes a directory', async () => {
    mount(loaders(ok(listing())))
    const tree = await screen.findByRole('tree', { name: 'Files in this checkpoint' })
    const controls = within(tree).getAllByRole('button')
    expect(controls.map((b) => b.textContent)).toEqual([
      '▾ bin/',
      'run.sh',
      '▾ progress/',
      'step-0001.md',
      'state.json',
    ])

    controls[0]!.focus()
    fireEvent.keyDown(controls[0]!, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(controls[1])
    fireEvent.keyDown(controls[1]!, { key: 'End' })
    expect(document.activeElement).toBe(controls[4])
    fireEvent.keyDown(controls[4]!, { key: 'Home' })
    expect(document.activeElement).toBe(controls[0])
    fireEvent.keyDown(controls[0]!, { key: 'ArrowUp' })
    expect(document.activeElement, 'Up from the first row went somewhere').toBe(controls[0])

    // Left from a file goes to its directory; Left on an open directory shuts
    // it; Right opens it again.
    fireEvent.keyDown(controls[1]!, { key: 'ArrowLeft' })
    expect(document.activeElement).toBe(controls[0])
    fireEvent.keyDown(controls[0]!, { key: 'ArrowLeft' })
    expect(within(tree).getByRole('button', { name: '▸ bin/' })).toBeTruthy()
    expect(within(tree).queryByRole('button', { name: 'run.sh' })).toBeNull()
    fireEvent.keyDown(within(tree).getByRole('button', { name: '▸ bin/' }), { key: 'ArrowRight' })
    expect(within(tree).getByRole('button', { name: 'run.sh' })).toBeTruthy()
  })
})

describe('a refusal is drawn as a refusal', () => {
  /**
   * `content_type: null` is the allowlist's answer about the NAME, given
   * before a byte of the member was read. MUTATION: fall through to the
   * `binary` branch and this reads `not measured` / "not text" again.
   */
  it('draws the allowlist refusing a name as refused, with the server’s reason', async () => {
    const l = loaders(ok(listing({ files: [member('shot.png', { size: 48211 })], count: 1 })), (p) =>
      ok(
        content(p, null, {
          status: 'binary',
          content_type: null,
          total_bytes: 48211,
          returned_bytes: 0,
          detail:
            "this file's type is not on the text allowlist, so its bytes are not served here: nothing could scan them for a credential before they left. Download the whole checkpoint to read it.",
        }),
      ),
    )
    mount(l)
    fireEvent.click(await screen.findByRole('button', { name: 'shot.png' }))
    const viewer = await screen.findByRole('region', { name: 'Artifact shot.png' })

    expect(await within(viewer).findByText(/^refused/)).toBeTruthy()
    expect(viewer.textContent).toContain('not on the text allowlist')
    expect(viewer.textContent, 'a refusal drawn as an absence').not.toContain('not measured')
    expect(viewer.textContent, '"not text" is a claim about bytes nobody read').not.toMatch(/not text/)
    expect(viewer.querySelector('pre')).toBeNull()
  })

  /**
   * A 413 is the server declining a member that lies past this request's read
   * budget -- it says so, and says the download is the way to it. MUTATION:
   * route it through the failed-read panel and it grows a `try again`.
   */
  it('draws a read-budget refusal as refused, with no retry', async () => {
    const l = loaders(ok(listing()), () => ({
      status: 'error',
      error: {
        kind: 'server_error',
        httpStatus: 413,
        code: 'checkpoint_scan_budget_exceeded',
        message:
          "'state.json' could not be read within this request's scan budget. It may well be in the archive; download the whole checkpoint to read it.",
      },
    }))
    mount(l)
    fireEvent.click(await screen.findByRole('button', { name: 'state.json' }))
    const viewer = await screen.findByRole('region', { name: 'Artifact state.json' })

    expect(await within(viewer).findByText(/^refused/)).toBeTruthy()
    expect(viewer.textContent).toContain('scan budget')
    expect(
      within(viewer).queryByRole('button', { name: 'try again' }),
      'retrying a refusal can only get the same refusal',
    ).toBeNull()
    expect(viewer.textContent).not.toMatch(/API failed/)
  })
})

// ---------------------------------------------------------------------------
// Console visual QA pass 1 (QA1-FIX-09; part of #2596)
// ---------------------------------------------------------------------------
//
// Measured on the fixture console at phone-390 (sweep lane ovag): the
// ckpt_0002 row said Age `9d ago` and the panel opened from it said `archive
// 17.6 MiB · written 4m ago` -- two ages for one archive; `entries 8 partial
// entry cap` sat beside `manifest 184 files` without saying 8 of 184 were
// shown; and `files` on the first row opened its panel below the LAST row,
// out of view, with nothing on screen changing.

describe('one age, a cut said as N of M, and a panel that comes into view (QA1-FIX-09)', () => {
  const entriesFact = () =>
    [...region().querySelectorAll<HTMLElement>('.ctl-facts .ctl-fact')].find(
      (f) => (f.querySelector('b')?.textContent ?? '') === 'entries',
    )!

  /**
   * MUTATION: put the fixture's `object_updated_at` back to `Date.now() - 4
   * * 60_000`. The archive then says `written 4m ago` under a row whose Age
   * says `9d ago`, and the two times below differ.
   */
  it('the fixture archive was written when its checkpoint was, not four minutes ago', async () => {
    const { loadCheckpoints } = await import('../api')
    const page = await loadCheckpoints('task_fixture')
    if (page.status !== 'ok') throw new Error(`checkpoint fixture answered ${page.status}`)
    const row = page.data.checkpoints.find((c) => c.checkpoint_id === 'ckpt_0002')!
    expect(row.created_at).not.toBeNull()
    const files = await loadCheckpointFiles('task_fixture', row.attempt_id, 'ckpt_0002', { fixtures: true })
    if (files.status !== 'ok') throw new Error(`files fixture answered ${files.status}`)
    expect(files.data.archive.object_updated_at).toBe(row.created_at)
  })

  /**
   * MUTATION: draw `{data.count}` alone in the entries fact again. It reads
   * `5` beside `manifest 184 files`, and `4 of 184 files shown` is not on
   * the screen. MUTATION: draw `5 shown · 4 of 184 files` (the re-review's
   * minor): two different numbers side by side, the entry count counting the
   * directory, so the count is asserted alone.
   */
  it('says a listing the entry cap cut as the manifest\'s files: N of M files shown', async () => {
    mount(
      loaders(
        ok(
          listing({
            truncated: true,
            truncated_reason: 'entry_cap',
            file_count_agrees: null,
            manifest: { ...listing().manifest, file_count: 184 },
          }),
        ),
      ),
    )
    await screen.findByRole('tree')
    // Five entries, one of them a directory: the manifest counts files, so
    // the comparison is the four non-directory members against its 184.
    expect(entriesFact().textContent).toContain('4 of 184 files shown')
    expect(entriesFact().textContent).not.toContain('5')
    expect(within(entriesFact()).getByText('entry cap')).toBeTruthy()
  })

  it('says a cut listing with no manifest count as N shown, and claims no total', async () => {
    mount(
      loaders(
        ok(
          listing({
            truncated: true,
            truncated_reason: 'entry_cap',
            file_count_agrees: null,
            manifest: { ...listing().manifest, status: 'unreadable', file_count: null },
          }),
        ),
      ),
    )
    await screen.findByRole('tree')
    expect(entriesFact().textContent).toContain('5 shown')
    expect(entriesFact().textContent).not.toMatch(/ of /)
  })

  it('draws a whole listing\'s entries as the bare count', async () => {
    mount(loaders(ok(listing())))
    await screen.findByRole('tree')
    expect(entriesFact().textContent).toBe('entries5')
  })

  /**
   * MUTATION: delete the mount effect that scrolls the region into view and
   * focuses it. `scrollIntoView` is never called and focus stays on the body
   * -- the panel opens off screen and the `files` button reads as dead.
   * MUTATION: scroll with `{ block: 'nearest' }`. Only the panel's top edge
   * comes into view (rendered at phone-390: heading and download button,
   * the `entries` line half under the pane's bottom edge), so the call's
   * argument is pinned to `start`.
   */
  it('scrolls the panel into view and moves focus to it when it opens', async () => {
    const scroll = vi.fn()
    const had = Object.prototype.hasOwnProperty.call(Element.prototype, 'scrollIntoView')
    const before = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scroll
    try {
      mount(loaders(ok(listing())))
      await waitFor(() => expect(scroll).toHaveBeenCalled())
      expect(scroll).toHaveBeenCalledWith({ block: 'start' })
      expect(scroll.mock.contexts[0]).toBe(region())
      expect(document.activeElement).toBe(region())
    } finally {
      if (had) Element.prototype.scrollIntoView = before
      else delete (Element.prototype as Partial<Element>).scrollIntoView
    }
  })

  /**
   * THE SCROLL RUNS AGAIN ONCE THE LISTING HAS DRAWN (QA1-FIX-09-FIX, the
   * re-review's finding). At mount the panel is its 106 px loading state, so
   * the scroll container's maximum is computed from that short panel and the
   * scroll clamps there; the listing grows the panel to 457 px 200 ms later
   * and, with the scroll run only at mount, nothing scrolls again (rendered
   * at phone-390: the panel's top stayed at y=666 of 844). The loader is held
   * open here, so the mount-time call happens with no tree in the region.
   * MUTATION: key the scroll effect on `[]` again. Every call then finds the
   * region still loading, and no call sees the tree.
   */
  it('scrolls the panel into view again once its listing has drawn, not only on mount', async () => {
    const sawTree: boolean[] = []
    const scroll = vi.fn(function (this: Element) {
      sawTree.push(this.querySelector('[role="tree"]') !== null)
    })
    const had = Object.prototype.hasOwnProperty.call(Element.prototype, 'scrollIntoView')
    const before = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scroll
    let answer: (r: Result<CheckpointFiles>) => void = () => {}
    const l = loaders(ok(listing()))
    l.files.mockImplementation(() => new Promise<Result<CheckpointFiles>>((r) => (answer = r)))
    try {
      mount(l)
      await waitFor(() => expect(l.files).toHaveBeenCalled())
      expect(sawTree, 'a scroll ran with the listing already drawn').not.toContain(true)
      answer(ok(listing()))
      await screen.findByRole('tree')
      await waitFor(() => expect(sawTree, 'no scroll ran once the listing had drawn').toContain(true))
      expect(scroll.mock.calls.at(-1)).toEqual([{ block: 'start' }])
      expect(scroll.mock.contexts.at(-1)).toBe(region())
    } finally {
      if (had) Element.prototype.scrollIntoView = before
      else delete (Element.prototype as Partial<Element>).scrollIntoView
    }
  })
})
