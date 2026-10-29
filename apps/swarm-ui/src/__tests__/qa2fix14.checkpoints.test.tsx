// CONSOLE VISUAL QA PASS 2, LANE QA2-FIX-14 (2026-10-29, part of #2694):
// Work › Agents › agent › Checkpoints, as the fixture console drew it on main.
//
//   overview-agents-48  Escape on a checkpoint's files panel closed the whole
//                       agent pane. The panel is an escape layer now: the key
//                       closes it, and never reaches the pane's listener. A
//                       layer opened above it (the file viewer) still closes
//                       first.
//
// The pane here is a stand-in for AgentSplit, which closes on its own
// `onKeyDown` -- a bubble listener the panel sits inside.
//
// MUTATION: drop `useEscapeLayer(true, onClose)` from `CheckpointBrowser` and
// the first test goes red: the pane closes, the panel stays. The second holds
// the fix to the stack's order: register the panel on every render (a new
// layer pushed after the viewer's) and it goes red.

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { CheckpointBrowser, type CheckpointFiles, type CheckpointLoaders } from '../CheckpointBrowser'
import { useEscapeLayer } from '../escape'

afterEach(cleanup)

// The listing never answers: what Escape does does not depend on it.
const PENDING: CheckpointLoaders = {
  files: () => new Promise(() => {}),
  file: () => new Promise(() => {}),
} as unknown as CheckpointLoaders

function Above({ onClose }: { onClose: () => void }) {
  useEscapeLayer(true, onClose)
  return null
}

function mount(onClose: () => void, onPane: () => void) {
  return render(
    <div data-testid="pane" onKeyDown={(e) => e.key === 'Escape' && onPane()}>
      <CheckpointBrowser taskId="task_a" attemptId="att_1" checkpointId="ckpt-00001" onClose={onClose} loaders={PENDING} />
    </div>,
  )
}

const region = () => screen.getByRole('region', { name: 'Checkpoint ckpt-00001 files' })

describe('overview-agents-48: Escape on the files panel', () => {
  it('closes the panel, and the pane around it stays open', () => {
    const close = vi.fn()
    const pane = vi.fn()
    mount(close, pane)
    fireEvent.keyDown(region(), { key: 'Escape' })
    expect(close).toHaveBeenCalledTimes(1)
    expect(pane).not.toHaveBeenCalled()
  })

  it('leaves a layer opened above the panel to close first', () => {
    const close = vi.fn()
    const pane = vi.fn()
    const above = vi.fn()
    const { rerender } = mount(close, pane)
    rerender(
      <div data-testid="pane" onKeyDown={(e) => e.key === 'Escape' && pane()}>
        <CheckpointBrowser taskId="task_a" attemptId="att_1" checkpointId="ckpt-00001" onClose={close} loaders={PENDING} />
        <Above onClose={above} />
      </div>,
    )
    fireEvent.keyDown(region(), { key: 'Escape' })
    expect(above).toHaveBeenCalledTimes(1)
    expect(close).not.toHaveBeenCalled()
    expect(pane).not.toHaveBeenCalled()
  })
})

const PREFIX = 'tenants/eng/tasks/task_a/attempts/att_1/checkpoints/ckpt-00001'

// One file, listed; reading it never answers, so its viewer stays open.
const LISTED: CheckpointLoaders = {
  files: async () => ({
    status: 'ok',
    data: {
      task_id: 'task_a',
      tenant_id: 'eng',
      attempt_id: 'att_1',
      checkpoint_id: 'ckpt-00001',
      prefix: PREFIX,
      archive: { key: `${PREFIX}/archive.tar.gz`, uri: `gs://bucket/${PREFIX}/archive.tar.gz`, bytes: 4096 },
      manifest: {
        status: 'present',
        detail: null,
        file_count: 1,
        archive_bytes: 4096,
        archive_sha256: 'abc',
        created_at: null,
        label: 'periodic',
        archive_key_agrees: true,
      },
      status: 'ok',
      detail: null,
      files: [{ path: 'state.json', size: 23, mode: 0o644, type: 'file', link: null, unsafe: false, undecodable: false }],
      count: 1,
      truncated: false,
      truncated_reason: null,
      file_count_agrees: true,
      scanned_bytes: 4096,
    } as CheckpointFiles,
  }),
  file: () => new Promise(() => {}),
} as unknown as CheckpointLoaders

function hosted(hidden: boolean, onClose: () => void, onPane: () => void, loaders: CheckpointLoaders) {
  return (
    <div data-testid="pane" tabIndex={-1} onKeyDown={(e) => e.key === 'Escape' && onPane()}>
      <div hidden={hidden}>
        <CheckpointBrowser taskId="task_a" attemptId="att_1" checkpointId="ckpt-00001" onClose={onClose} loaders={loaders} />
      </div>
    </div>
  )
}

// The panel learns it was hidden or shown from a MutationObserver, whose
// callback runs as a microtask after the attribute changes.
const flush = () => act(async () => {})
const viewer = () => screen.queryByRole('region', { name: 'Artifact state.json' })

describe('overview-agents-48 (review): the panel is a layer only while it is shown', () => {
  it('leaves Escape to the agent pane while the Checkpoints tab is hidden', async () => {
    const close = vi.fn()
    const pane = vi.fn()
    const { rerender } = render(hosted(false, close, pane, PENDING))
    rerender(hosted(true, close, pane, PENDING))
    await flush()
    fireEvent.keyDown(screen.getByTestId('pane'), { key: 'Escape' })
    expect(close).not.toHaveBeenCalled()
    expect(pane).toHaveBeenCalledTimes(1)
    // Shown again, it closes first once more.
    rerender(hosted(false, close, pane, PENDING))
    await flush()
    fireEvent.keyDown(region(), { key: 'Escape' })
    expect(close).toHaveBeenCalledTimes(1)
    expect(pane).toHaveBeenCalledTimes(1)
  })

  it('keeps an open file above the panel after the tab is hidden and shown again', async () => {
    const close = vi.fn()
    const pane = vi.fn()
    const { rerender } = render(hosted(false, close, pane, LISTED))
    fireEvent.click(await screen.findByRole('button', { name: 'state.json' }))
    expect(viewer()).not.toBeNull()
    rerender(hosted(true, close, pane, LISTED))
    await flush()
    rerender(hosted(false, close, pane, LISTED))
    await flush()
    fireEvent.keyDown(region(), { key: 'Escape' })
    expect(close).not.toHaveBeenCalled()
    expect(pane).not.toHaveBeenCalled()
    await waitFor(() => expect(viewer()).toBeNull())
    // The file closed; the next Escape closes the panel.
    fireEvent.keyDown(region(), { key: 'Escape' })
    expect(close).toHaveBeenCalledTimes(1)
  })
})
