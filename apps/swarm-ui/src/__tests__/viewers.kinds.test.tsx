// THE VIEWERS, ONE PER KIND (viewers.html, pick A, 2026-10-02).
//
// The kind is the server's; the viewer never guesses. JSON opens as a tree
// with a Raw toggle and the server's mask drawn in its own ink; markdown is
// Rendered or Source and never HTML built from agent bytes; a log is numbered
// lines in one <pre>; a binary file has no preview and a download that says it
// is not redacted; a window that is not the whole file says so and offers the
// next one; a member past one request's budget is a 413 refusal, not a
// failure.
//
// MUTATIONS, one per block: default JSON to Raw; drop the mask's run; render
// markdown source by default; drop the line numbers; drop "not redacted" from
// the binary download; draw a 413 as a failed read.

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { ArtifactContent, ArtifactKindName } from '../types'

const api = vi.hoisted(() => ({ loadArtifactContent: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { ArtifactViewer } = await import('../ArtifactViewer')

const MASK = '*'.repeat(8)

function content(name: string, text: string | null, over: Partial<ArtifactContent> = {}): ArtifactContent {
  return {
    task_id: 'tsk_v',
    tenant_id: 'acme',
    attempt_id: 'att_3',
    artifact: { name, bytes: text?.length ?? 0, uri: `gs://b/${name}` },
    status: 'ok',
    detail: null,
    key: 'k',
    uri: `gs://b/${name}`,
    content: text,
    total_bytes: text?.length ?? 0,
    offset: 0,
    returned_bytes: text?.length ?? 0,
    next_offset: null,
    truncated: false,
    redacted: false,
    redaction_count: 0,
    redaction: { applied_at_read_time: true, rules: 9 },
    ...over,
  }
}

async function view(data: ArtifactContent, kind: ArtifactKindName | null = null): Promise<HTMLElement> {
  const got: Result<ArtifactContent> = { status: 'ok', data, fetchedAt: Date.now() }
  api.loadArtifactContent.mockResolvedValue(got)
  const name = data.artifact.name ?? 'x'
  render(<ArtifactViewer taskId="tsk_v" artifact={{ name, bytes: 1, uri: `gs://b/${name}` }} onClose={() => {}} kind={kind} />)
  return waitFor(() => {
    const el = screen.getByRole('region', { name: `Artifact ${name}` })
    expect(el.querySelector('.art-loading')).toBeNull()
    return el
  })
}

describe('JSON: a tree with a Raw toggle, and the mask in its own ink', () => {
  it('opens a whole file as a tree that counts an array’s items', async () => {
    const json = JSON.stringify({ suite: 'scheduler', runs: 50, cases: [{ a: 1 }, { a: 2 }], token: MASK })
    const el = await view(content('flake-report.json', json, { redacted: true, redaction_count: 1 }), 'json')
    const tree = within(el).getByRole('tree', { name: 'JSON tree' })
    expect(tree.textContent).toContain('"suite": "scheduler"')
    expect(tree.textContent).toContain('[ 2 items ]')
    expect(tree.querySelector('.ag-mask')?.textContent).toBe(MASK)
    const views = within(el).getByRole('group', { name: 'JSON view' })
    expect(within(views).getByRole('button', { name: 'Tree' }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(within(views).getByRole('button', { name: 'Raw' }))
    expect(el.querySelector('pre.art-text')?.textContent).toBe(JSON.stringify(JSON.parse(json), null, 2))
  })
})

describe('markdown: rendered as React elements, or its source', () => {
  it('renders by default, and shows the source on request', async () => {
    const el = await view(content('SUMMARY.md', '# Fix\n\nThe renew path.\n'), 'markdown')
    expect(el.querySelector('.art-md h4')?.textContent).toBe('Fix')
    fireEvent.click(within(el).getByRole('button', { name: 'Source' }))
    expect(el.querySelector('pre.art-text')?.textContent).toBe('# Fix\n\nThe renew path.\n')
    expect(el.querySelector('.art-md')).toBeNull()
  })
})

describe('a log: numbered lines, in one <pre>', () => {
  it('numbers each line and keeps the window copyable as one block', async () => {
    const el = await view(content('pytest-full.log', 'collected 214 items\n214 passed\n'), 'log')
    const pre = el.querySelector('pre.logwin-body')!
    expect([...pre.querySelectorAll('.ag-logno')].map((n) => n.textContent)).toEqual(['1', '2'])
    expect(pre.querySelectorAll('.ag-logline')).toHaveLength(2)
  })
})

describe('a window that is not the whole file says so, and offers the next', () => {
  it('says partial with the byte it continues from', async () => {
    const el = await view(
      content('pytest-full.log', 'x\n'.repeat(10), { truncated: true, next_offset: 524_288, total_bytes: 3_984_588 }),
      'log',
    )
    expect(el.querySelector('.art-prov')?.textContent).toMatch(/partial|window/)
    expect(within(el).getByRole('button', { name: 'next window' })).toBeTruthy()
  })
})

describe('binary: no preview, and a download that says it is not redacted', () => {
  it('reads nothing, downloads as stored, and says no rule scanned it', async () => {
    const el = await view(content('coverage.sqlite', null, { status: 'binary', total_bytes: 6_400_000 }), 'binary')
    expect(el.textContent).toMatch(/No preview/)
    const download = within(el).getByRole('link', { name: 'Download' })
    expect(download.getAttribute('href')).toContain('/v1/tasks/tsk_v/artifacts/raw?')
    expect(download.getAttribute('href')).toContain('disposition=attachment')
    expect(el.querySelector('.art-binary-say')?.textContent).toMatch(/not redacted/)
  })
})

describe('a member past one request’s read budget is a refusal, not a failure', () => {
  it('draws the 413 as refused, with the server’s words, and no "try again"', async () => {
    api.loadArtifactContent.mockResolvedValue({
      status: 'error',
      error: { kind: 'server_error', httpStatus: 413, code: 'too_large', message: 'past this request’s read budget; download the checkpoint' },
    })
    render(<ArtifactViewer taskId="tsk_v" artifact={{ name: 'workspace/lib.so', bytes: 1, uri: null as unknown as string }} onClose={() => {}} />)
    const el = await screen.findByRole('region', { name: 'Artifact workspace/lib.so' })
    await waitFor(() => expect(el.textContent).toMatch(/read budget/))
    expect(within(el).queryByRole('button', { name: 'try again' })).toBeNull()
  })
})
