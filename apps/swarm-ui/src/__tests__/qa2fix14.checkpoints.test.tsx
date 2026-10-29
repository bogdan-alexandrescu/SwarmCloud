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

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { CheckpointBrowser, type CheckpointLoaders } from '../CheckpointBrowser'
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
