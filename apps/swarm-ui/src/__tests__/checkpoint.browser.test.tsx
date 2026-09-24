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
  return { path, size: 10, mode: 0o644, type: 'file', link: null, unsafe: false, ...over }
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

  it('shows a binary member as not text, with no bytes', async () => {
    const l = loaders(ok(listing({ files: [member('shot.png', { size: 48211 })], count: 1 })), (p) =>
      ok(content(p, null, { status: 'binary', content_type: null, total_bytes: 48211, returned_bytes: 0 })),
    )
    mount(l)
    fireEvent.click(await screen.findByRole('button', { name: 'shot.png' }))
    const viewer = await screen.findByRole('region', { name: 'Artifact shot.png' })
    expect(await within(viewer).findByText(/not text/)).toBeTruthy()
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
// The four answers
// ---------------------------------------------------------------------------

describe('absence and failure are not an empty checkpoint', () => {
  it('draws an ABSENT archive as missing, never as zero files', async () => {
    mount(loaders(ok(listing({ status: 'absent', files: null, count: 0, detail: 'no archive object' }))))

    expect(await screen.findByText(/archive not in the bucket/)).toBeTruthy()
    expect(within(region()).getByText('not measured')).toBeTruthy()
    expect(screen.queryByRole('tree')).toBeNull()
    expect(region().textContent).not.toMatch(/0 files/)
    expect(region().textContent).not.toContain('real zero')
    // Nothing to download: the server said so.
    expect(within(region()).queryByRole('link', { name: 'download archive' })).toBeNull()
  })

  it('draws a complete, empty archive as a measured zero', async () => {
    mount(loaders(ok(listing({ files: [], count: 0, file_count_agrees: true }))))

    expect(await screen.findByText(/^\s*0 files$/)).toBeTruthy()
    expect(within(region()).getByText('real zero')).toBeTruthy()
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
    expect(region().textContent).not.toContain('real zero')
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
