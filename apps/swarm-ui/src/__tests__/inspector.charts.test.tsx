// The four inspector charts, mounted where they belong -- and the tables they
// sit above, still there.
//
// A chart that is perfect and unmounted is the defect `mounted.test.tsx`
// exists for; a chart that REPLACED its table would remove the facts a reader
// copies (ids, uris, subjects, exact counts). Both are asserted here, on the
// real `Run` component, so deleting a chart from `AgentDetail.tsx` or deleting
// a table to make room for one turns this red.

import { render, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { AgentRun } from '../api'
import { at, attempt, ev, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { Run } from '../AgentDetail'

function run(): AgentRun {
  const t = task({
    state: 'SUCCEEDED',
    attempt_count: 2,
    result_summary: {
      git: {
        base: 'd41f0c9a7b',
        commits: [
          {
            sha: '9b1c7f00aa11', subject: 'Harden the guard', author: 'agent',
            committed_at: at(18), files_changed: 3, insertions: 61, deletions: 12, binary_files: 0,
          },
          {
            sha: '7a2b3c4d5e66', subject: 'Add the logo', author: 'agent',
            committed_at: at(19), files_changed: 1, insertions: 0, deletions: 0, binary_files: 1,
          },
        ],
        commit_count: 2,
        insertions: 61,
        deletions: 12,
        dirty: [],
        patch: null,
      },
      artifacts: [],
      logs: {},
    },
  })
  const attempts = [
    attempt(1, { created_at: at(0), started_at: at(1), completed_at: at(6), exit_code: 1 }),
    attempt(2, {
      created_at: at(10),
      started_at: at(13),
      completed_at: at(20),
      checkpoints: ['ck_1', 'ck_2'],
      peak_rss_bytes: 2_000_000_000,
    }),
  ]
  return {
    task: t,
    events: [
      ev('ready', at(8), null),
      ev('heartbeat', at(15), 'att_2', { peak_rss_bytes: 900_000_000 }),
      ev('heartbeat', at(17.5), 'att_2', { peak_rss_bytes: 1_400_000_000 }),
      ev('checkpoint_completed', at(16), 'att_2', {
        checkpoint_id: 'ck_1', uri: 'gs://acme/ck_1', size_bytes: 40_000_000, seq: 1,
      }),
    ],
    eventsDetail: null,
    attempts,
    attemptsDetail: null,
    classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
  }
}

async function mount(): Promise<HTMLElement> {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const { container } = render(<Run run={run()} />)
  await waitFor(() => expect(container.querySelector('.ctl-metrics')).not.toBeNull())
  return container as HTMLElement
}

describe('the inspector draws its charts and keeps its tables', () => {
  it('draws the phase bars, the retry lollipop and the summed work in the attempts panel', async () => {
    const el = await mount()
    const fig = el.querySelector('figure.ctl-phases')
    expect(fig, 'the phase chart is not mounted').not.toBeNull()
    expect(fig!.querySelectorAll('[data-testid="phase-row"]')).toHaveLength(2)
    expect(fig!.querySelectorAll('[data-testid="lolly"]')).toHaveLength(2)
    // 5 minutes of attempt 1 plus 7 of attempt 2.
    expect(fig!.querySelector('[data-testid="work-sum"]')?.textContent).toContain('ran 12m 0s over 2 of 2')
    // The cards still state each attempt's own start and end as facts.
    expect(el.querySelectorAll('.att-card').length).toBeGreaterThanOrEqual(2)
  })

  it('draws the peak-memory step line and keeps the requested-vs-utilised bar', async () => {
    const el = await mount()
    const peaks = el.querySelectorAll('figure.ctl-peak')
    // Attempt 1 has no heartbeat on the page, so only attempt 2 draws one.
    expect(peaks).toHaveLength(1)
    expect(peaks[0]!.querySelector('[data-testid="step-line"]')).not.toBeNull()
    expect(el.querySelectorAll('.ctl-util').length).toBeGreaterThanOrEqual(3)
  })

  it('draws the checkpoint strip ABOVE the checkpoint table, which is still there', async () => {
    const el = await mount()
    const strip = el.querySelector('figure.ctl-ckpt-strip')
    expect(strip, 'the checkpoint strip is not mounted').not.toBeNull()
    const table = strip!.nextElementSibling
    expect(table?.querySelector('th')?.textContent).toBe('Checkpoint')
    // ck_2 has no event on the page: in the tray, and still a table row.
    expect(strip!.querySelector('[data-testid="ckpt-offpage"][data-id="ck_2"]')).not.toBeNull()
    expect(table?.textContent).toContain('ck_2')
  })

  it('draws the diffstat ABOVE the commit table, which is still there', async () => {
    const el = await mount()
    const chart = el.querySelector('figure.ctl-diffstat')
    expect(chart, 'the diffstat is not mounted').not.toBeNull()
    expect(chart!.querySelectorAll('[data-testid="diff-row"]')).toHaveLength(2)
    const table = chart!.nextElementSibling
    expect(table?.textContent).toContain('Harden the guard')
    expect(table?.textContent).toContain('+61 −12')
  })
})
