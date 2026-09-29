// CH-13, THE MARKUP HALF: A LONG VALUE THAT ELLIPSIZES KEEPS ITS WHOLE SELF.
//
// Below 900px a record-like table stacks, and a gs:// uri in it used to wrap
// over nine lines at 390. It ellipsizes now (the cascade half is in
// `chrome.shared.test.tsx`), and a value that is cut on screen has to be
// whole somewhere a reader can reach: in its `title`, and in the copy action
// beside it -- the AH-11 precedent. A cut uri is a different uri.
//
// Rendered through the inspector's own `Run`, with the two loaders its file
// panel calls stubbed, as `checkpoint.reclaimed.test.tsx` does.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { AgentRun } from '../api'
import type { CheckpointsPage } from '../types'
import { at, attempt, ev, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run } = await import('../AgentDetail')

const URI = 'gs://swarm-artifacts/tenants/eng/tasks/tsk_charts/attempts/att_1/checkpoints/ckpt-00001/'

function agentRun(): AgentRun {
  return {
    task: task({ state: 'SUCCEEDED', attempt_count: 1 }),
    events: [ev('checkpoint_completed', at(5), 'att_1', { checkpoint_id: 'ckpt-00001', uri: URI, size_bytes: 2048, seq: 1 })],
    eventsDetail: null,
    attempts: [attempt(1, { checkpoints: ['ckpt-00001'] })],
    attemptsDetail: null,
    classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
  }
}

function listing(): CheckpointsPage {
  return {
    task_id: 'tsk_charts',
    tenant_id: 'acme',
    prefix: 'tenants/eng/tasks/tsk_charts/attempts/',
    checkpoints: [
      {
        checkpoint_id: 'ckpt-00001',
        attempt_id: 'att_1',
        attempt_known: true,
        attempt_created_at: null,
        attempt_completed_at: null,
        prefix: 'tenants/eng/tasks/tsk_charts/attempts/att_1/checkpoints/ckpt-00001/',
        uri: URI,
        is_latest_pointer: false,
        objects: [],
        stored_bytes: 2048,
        manifest: 'present',
        manifest_detail: null,
        created_at: at(5),
        seq: 1,
        generation: 1,
        label: null,
        archive_bytes: null,
        archive_sha256: null,
        file_count: null,
        resumable: true,
        resumable_detail: null,
      },
    ],
    count: 1,
    total_found: 1,
    next_page_token: null,
    listed: true,
    truncated: false,
    latest_checkpoint: { pointer: null, status: 'unset', checkpoint_id: null },
  }
}

describe('CH-13: a stacked record ellipsizes a long value and keeps it whole', () => {
  it("keeps a checkpoint's whole uri in its title, beside the action that copies it", async () => {
    // MUTATION: drop the `title` from the Location cell's uri.
    // #102: the checkpoints' one table is the Checkpoints section's, read from
    // the bucket, so the listing is what carries the uri now.
    api.loadCheckpoints.mockResolvedValue({ status: 'ok', fetchedAt: Date.now(), data: listing() })
    api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    const { container } = render(<Run run={agentRun()} />)
    const uri = await waitFor(
      () => {
        const el = [...container.querySelectorAll('.ctl-table.is-stacked .uri')].find((e) => e.textContent === URI)
        expect(el, 'the checkpoint table drew no Location uri').toBeTruthy()
        return el!
      },
      { timeout: 5000 },
    )
    expect(uri.getAttribute('title'), 'the cut uri is not whole in its title').toBe(URI)
    expect(uri.parentElement?.querySelector('button.copy'), 'no copy action beside the uri').not.toBeNull()
  })
})
