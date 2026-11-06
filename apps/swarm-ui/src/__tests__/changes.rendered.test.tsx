// THE CHANGES VIEW DRAWS AN AGENT'S MOCK-UP (BGD, 2026-11-06: "see the
// mockups produced by the workflow agents in here ... and not just the file
// name").
//
// An HTML page, an SVG or an image opens on Rendered, with a Rendered | Diff
// switch and Diff unchanged. The page is drawn ONLY in an iframe whose sandbox
// attribute is exactly empty, fed by srcdoc and never src; an image through an
// img from a Blob object URL revoked on unmount. The theme is set on the
// srcdoc's html element; an unreadable file says why and keeps Diff; a file
// over 5 MB is not rendered.
//
// MUTATIONS, one per block: give the iframe `allow-scripts` or a `src` (the
// sandbox block); drop the revoke (the image block); drop `withTheme` (the
// theme block); read the patch before the checkpoint (the checkpoint block);
// drop the cap check (the cap block).

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { deflateSync } from 'node:zlib'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { ArtifactContent, GitSummary, Task, Workflow, WorkflowStep } from '../types'
import { attempt, task as baseTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadArtifactContent: vi.fn(),
  loadWorkflow: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadCheckpoints: vi.fn(),
}))
const ckpt = vi.hoisted(() => ({ loadCheckpointFile: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})
vi.mock('../CheckpointBrowser', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../CheckpointBrowser')>()
  return { ...actual, ...ckpt }
})

const { WorkflowChangesTab } = await import('../WorkflowChanges')
const { frameViewport, withTheme, decodeGitBase85, RENDER_CAP_BYTES } = await import('../RenderedFile')

// ---------------------------------------------------------------------------
// Fixtures: one step that added a desktop frame, a phone frame, a PNG and a
// frames.json, and modified an existing page.
// ---------------------------------------------------------------------------

const PATCH = 'swarm-work.patch'
const DIR = 'docs/design/mocks/sp/frames'
const DESK = `${DIR}/sp1-a-rest.html`
const PHONE = `${DIR}/sp1-a-rest-390.html`
const PNG = `${DIR}/sp1-a-rest.png`
const OLD_PAGE = 'docs/site/index.html'

const PAGE = '<!doctype html>\n<html lang="en"><head><title>frame</title></head>\n<body><h1>Settings</h1><script>window.__agentRan = 1; parent.__agentRan = 1</script></body></html>'

function added(path: string, text: string): string {
  const lines = text.split('\n')
  return [`diff --git a/${path} b/${path}`, 'new file mode 100644', '--- /dev/null', `+++ b/${path}`, `@@ -0,0 +1,${lines.length} @@`, ...lines.map((l) => `+${l}`)].join('\n') + '\n'
}

function modified(path: string): string {
  return [`diff --git a/${path} b/${path}`, `--- a/${path}`, `+++ b/${path}`, '@@ -1,1 +1,1 @@', '-<p>old</p>', '+<p>new</p>'].join('\n') + '\n'
}

const B85 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz!#$%&()*+-;<=>?@^_`{|}~'

/** Git's base85 lines, as `git diff --binary` writes them. */
function base85Lines(data: Uint8Array): string[] {
  const out: string[] = []
  for (let at = 0; at < data.length; at += 52) {
    const chunk = data.slice(at, at + 52)
    const padded = new Uint8Array(Math.ceil(chunk.length / 4) * 4)
    padded.set(chunk)
    let line = chunk.length <= 26 ? String.fromCharCode(64 + chunk.length) : String.fromCharCode(96 + chunk.length - 26)
    for (let i = 0; i < padded.length; i += 4) {
      let acc = ((padded[i]! << 24) >>> 0) + (padded[i + 1]! << 16) + (padded[i + 2]! << 8) + padded[i + 3]!
      const group: string[] = []
      for (let j = 0; j < 5; j++) {
        group.unshift(B85[acc % 85]!)
        acc = Math.floor(acc / 85)
      }
      line += group.join('')
    }
    out.push(line)
  }
  return out
}

const PNG_BYTES = Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, ...Array.from({ length: 70 }, (_, i) => (i * 37) % 256)])

function binaryAdded(path: string, bytes: Uint8Array): string {
  const packed = new Uint8Array(deflateSync(bytes))
  return (
    [
      `diff --git a/${path} b/${path}`,
      'new file mode 100644',
      'index 0000000..1111111',
      'GIT binary patch',
      `literal ${bytes.length}`,
      ...base85Lines(packed),
      '',
      'literal 0',
      'HcmV?d00001',
      '',
    ].join('\n') + '\n'
  )
}

const FRAMES_JSON = JSON.stringify([{ name: 'sp1-a-rest-390', viewport: { width: 390, height: 844 } }])

const PATCH_TEXT = added(DESK, PAGE) + added(PHONE, PAGE) + binaryAdded(PNG, PNG_BYTES) + added(`${DIR}/frames.json`, FRAMES_JSON) + modified(OLD_PAGE)

type FileRow = NonNullable<GitSummary['files']>[number]
const row = (path: string, insertions: number | null, deletions: number | null, binary = false): FileRow => ({ path, old_path: null, status: 'A', insertions, deletions, binary })
const GIT: GitSummary = {
  base: 'abc',
  patch: PATCH,
  files: [row(DESK, 3, 0), row(PHONE, 3, 0), row(PNG, null, null, true), row(`${DIR}/frames.json`, 1, 0), row(OLD_PAGE, 1, 1)],
}

function stepTask(id: string, git: GitSummary = GIT): Task {
  return baseTask({ id, state: 'SUCCEEDED', workflow_id: 'wf_one', result_summary: { git, artifacts: [{ name: PATCH, bytes: 100, uri: 'gs://b/p' }] } })
}

function step(step_id: string, task_id: string): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id }
}

function wf(steps: WorkflowStep[]): Workflow {
  return {
    workflow_id: 'wf_one', state: 'SUCCEEDED', tenant_id: 'eng', stored_state: 'SUCCEEDED',
    created_at: '2026-10-08T10:00:00Z', updated_at: '2026-10-08T11:00:00Z', submitted_by: 'operator@example.com',
    priority: 0, on_step_failure: 'FAIL_WORKFLOW', cancel_requested: false, steps,
  } as Workflow
}

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function served(taskId: string, content: string): ArtifactContent {
  return {
    task_id: taskId, tenant_id: 'eng', attempt_id: 'att_1', artifact: { name: PATCH, bytes: content.length, uri: null },
    status: 'ok', detail: null, key: 'k', uri: null, content, total_bytes: content.length, offset: 0,
    returned_bytes: content.length, next_offset: null, truncated: false, redacted: true, redaction_count: 0,
    redaction: { applied_at_read_time: true, rules: 9 },
  }
}

/** A checkpoint member as the route serves it. */
function member(path: string, content: string | null, over: Record<string, unknown> = {}) {
  const bytes = content === null ? 0 : new TextEncoder().encode(content).length
  return ok({
    ...served('t_mock', content ?? ''),
    content,
    total_bytes: bytes,
    returned_bytes: bytes,
    checkpoint_id: 'ckpt-00002',
    path,
    content_type: 'text/html',
    member: { type: 'file', mode: 0o644, size: bytes },
    ...over,
  })
}

const NO_CHECKPOINTS = ok({ checkpoints: [], latest_checkpoint: { pointer: null, status: 'unset', checkpoint_id: null } })
const ONE_CHECKPOINT = ok({
  checkpoints: [{ checkpoint_id: 'ckpt-00002', attempt_id: 'att_1', is_latest_pointer: true }],
  latest_checkpoint: { pointer: 'p', status: 'present', checkpoint_id: 'ckpt-00002' },
})

function Tab({ file, git }: { file: string; git?: GitSummary }) {
  const [at, setAt] = useState<{ step: string | null; file: string | null }>({ step: null, file })
  return <WorkflowChangesTab workflow={wf([step('mockups', 't_mock')])} taskById={new Map([['t_mock', stepTask('t_mock', git)]])} at={at} onAt={setAt} />
}

async function frame(): Promise<HTMLIFrameElement> {
  return (await screen.findByTitle(/, rendered$/)) as HTMLIFrameElement
}

const created: string[] = []
const revoked: string[] = []

beforeEach(() => {
  vi.stubGlobal('innerWidth', 1280)
  api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1)] }))
  api.loadArtifactContent.mockImplementation(async (id: string) => ok(served(id, PATCH_TEXT)))
  api.loadWorkflowUsage.mockReturnValue(new Promise(() => {}))
  api.loadCheckpoints.mockResolvedValue(NO_CHECKPOINTS)
  ckpt.loadCheckpointFile.mockResolvedValue({ status: 'error', error: { kind: 'not-found', httpStatus: 404, code: null, message: 'not in the archive' } })
  created.length = 0
  revoked.length = 0
  let n = 0
  URL.createObjectURL = vi.fn(() => {
    const u = `blob:console/obj-${++n}`
    created.push(u)
    return u
  })
  URL.revokeObjectURL = vi.fn((u: string) => {
    revoked.push(u)
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
  delete (window as unknown as Record<string, unknown>).__agentRan
})

describe('an HTML frame opens Rendered, in a sandbox that is exactly empty', () => {
  it('draws the page in an iframe with sandbox="" and srcdoc, never src', async () => {
    render(<Tab file={DESK} />)
    const f = await frame()
    expect(f.getAttribute('sandbox')).toBe('')
    expect(f.hasAttribute('srcdoc')).toBe(true)
    expect(f.hasAttribute('src')).toBe(false)
    expect(f.getAttribute('srcdoc')).toContain('<h1>Settings</h1>')
    expect(f.getAttribute('srcdoc')).toContain("default-src 'none'")
    expect(screen.getByRole('button', { name: 'Rendered' }).getAttribute('aria-pressed')).toBe('true')
    expect(document.querySelector('object, embed')).toBeNull()
    // The agent's markup is not in the console's own DOM.
    expect(screen.queryByRole('heading', { name: 'Settings' })).toBeNull()
  })

  it('runs no script of the frame: no global is set, here or in the frame', async () => {
    render(<Tab file={DESK} />)
    const f = await frame()
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20))
    })
    expect((window as unknown as Record<string, unknown>).__agentRan).toBeUndefined()
    expect((f.contentWindow as unknown as Record<string, unknown> | null)?.__agentRan).toBeUndefined()
  })

  it('Diff is today’s view, unchanged', async () => {
    render(<Tab file={DESK} />)
    await frame()
    fireEvent.click(screen.getByRole('button', { name: 'Diff' }))
    expect(await screen.findByRole('region', { name: 'Diff lines' })).toBeTruthy()
    expect(screen.queryByTitle(/, rendered$/)).toBeNull()
  })

  it('reads the step’s own checkpoint before the patch', async () => {
    api.loadCheckpoints.mockResolvedValue(ONE_CHECKPOINT)
    ckpt.loadCheckpointFile.mockImplementation(async (_t: string, _a: string, _c: string, p: string) =>
      p === `repo/${DESK}` ? member(p, '<html><body><p>from the checkpoint</p></body></html>') : { status: 'error', error: { kind: 'not-found', httpStatus: 404, code: null, message: 'no' } },
    )
    render(<Tab file={DESK} />)
    const f = await frame()
    await waitFor(() => expect(f.getAttribute('srcdoc')).toContain('from the checkpoint'))
    expect(ckpt.loadCheckpointFile).toHaveBeenCalledWith('t_mock', 'att_1', 'ckpt-00002', `repo/${DESK}`)
  })
})

describe('light and dark', () => {
  it('the theme switch changes data-theme on the srcdoc’s html element', async () => {
    render(<Tab file={DESK} />)
    const f = await frame()
    expect(f.getAttribute('srcdoc')).toMatch(/<html data-theme="light" lang="en">/)
    fireEvent.click(screen.getByRole('button', { name: 'Dark' }))
    await waitFor(() => expect((screen.getByTitle(/, rendered$/) as HTMLIFrameElement).getAttribute('srcdoc')).toMatch(/<html data-theme="dark" lang="en">/))
  })

  it('replaces a theme the page already set, and wraps a fragment', () => {
    expect(withTheme('<html data-theme="dark"><body/></html>', 'light')).toBe('<html data-theme="light"><body/></html>')
    expect(withTheme('<p>x</p>', 'dark')).toBe('<html data-theme="dark"><p>x</p></html>')
  })
})

describe('a frame recorded at a viewport', () => {
  it('a -390 frame is sized to the phone and a 100% option is offered', async () => {
    render(<Tab file={PHONE} />)
    const f = await frame()
    expect(f.getAttribute('width')).toBe('390')
    expect(f.getAttribute('height')).toBe('844')
    expect(screen.getByRole('button', { name: '100%' })).toBeTruthy()
  })

  it('a desktop frame is 1440x900, and frames.json overrides the naming', () => {
    expect(frameViewport(DESK, null)).toEqual({ width: 1440, height: 900 })
    expect(frameViewport(DESK, JSON.stringify({ frames: [{ name: 'sp1-a-rest', viewport: '1280x720' }] }))).toEqual({ width: 1280, height: 720 })
    expect(frameViewport(PHONE, 'not json')).toEqual({ width: 390, height: 844 })
  })
})

describe('an image', () => {
  it('a .png draws an img from an object URL, revoked on unmount', async () => {
    const view = render(<Tab file={PNG} />)
    const img = (await screen.findByRole('img', { name: PNG })) as HTMLImageElement
    expect(img.getAttribute('src')).toMatch(/^blob:/)
    expect(created).toContain(img.getAttribute('src'))
    const blob = (URL.createObjectURL as ReturnType<typeof vi.fn>).mock.calls[0]![0] as Blob
    expect(blob.type).toBe('image/png')
    const read = await new Promise<ArrayBuffer>((res) => {
      const r = new FileReader()
      r.onload = () => res(r.result as ArrayBuffer)
      r.readAsArrayBuffer(blob)
    })
    expect(new Uint8Array(read)).toEqual(PNG_BYTES)
    expect(revoked).toEqual([])
    view.unmount()
    expect(revoked).toEqual([img.getAttribute('src')])
  })

  it('decodes git’s base85 exactly', () => {
    const data = Uint8Array.from({ length: 61 }, (_, i) => (i * 13) % 256)
    expect(decodeGitBase85(base85Lines(data))).toEqual(data)
  })
})

describe('what cannot be rendered says why, and Diff still works', () => {
  it('a modified page with no checkpoint shows its reason; Diff draws the patch', async () => {
    render(<Tab file={OLD_PAGE} />)
    const why = await screen.findByText(/^not read: /)
    expect(why.textContent).toMatch(/this step wrote no checkpoint/)
    expect(why.textContent).toMatch(/only the changed lines/)
    expect(screen.queryByTitle(/, rendered$/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Diff' }))
    expect(await screen.findByRole('region', { name: 'Diff lines' })).toBeTruthy()
  })

  it('a file over the 5 MB cap is not rendered', async () => {
    api.loadCheckpoints.mockResolvedValue(ONE_CHECKPOINT)
    ckpt.loadCheckpointFile.mockImplementation(async (_t: string, _a: string, _c: string, p: string) =>
      member(p, '<html></html>', { total_bytes: RENDER_CAP_BYTES + 1, truncated: true, next_offset: 1024 }),
    )
    render(<Tab file={DESK} />)
    expect((await screen.findByText(/over the 5 MB cap/)).textContent).toMatch(/^not rendered: /)
    expect(screen.queryByTitle(/, rendered$/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Diff' }))
    expect(await screen.findByRole('region', { name: 'Diff lines' })).toBeTruthy()
  })
})

describe('the dev fixture: one desktop frame, one phone frame and one PNG', () => {
  it('the fixture step’s mock-up patch renders all three', async () => {
    const actual = await vi.importActual<typeof import('../api')>('../api')
    const tasks = await actual.loadTasks()
    expect(tasks.status === 'ok' || tasks.status === 'stale').toBe(true)
    const t = tasks.status === 'ok' || tasks.status === 'stale' ? tasks.data.tasks.find((x) => x.id === 'task_wf_scan_a') : undefined
    const git = (t?.result_summary as { git?: GitSummary } | null)?.git
    expect(git?.patch).toBe('mockups.patch')
    const patch = await actual.loadArtifactContent('task_wf_scan_a', 'mockups.patch')
    const text = patch.status === 'ok' ? patch.data.content : null
    expect(text).toContain('GIT binary patch')
    api.loadArtifactContent.mockImplementation(async (id: string) => ok(served(id, text ?? '')))
    const files = (git?.files ?? []).map((f) => f.path)
    const desk = files.find((p) => p.endsWith('rest.html'))!
    const phone = files.find((p) => p.endsWith('-390.html'))!
    const png = files.find((p) => p.endsWith('.png'))!

    const one = render(<Tab file={desk} git={git} />)
    expect((await frame()).getAttribute('width')).toBe('1440')
    one.unmount()
    const two = render(<Tab file={phone} git={git} />)
    expect((await frame()).getAttribute('width')).toBe('390')
    two.unmount()
    render(<Tab file={png} git={git} />)
    const img = await screen.findByRole('img', { name: png })
    expect(img.getAttribute('src')).toMatch(/^blob:/)
  })
})
