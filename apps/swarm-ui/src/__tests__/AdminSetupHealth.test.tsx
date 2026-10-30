// ADMIN › SETUP › HEALTH (DAYONE-SETUP-UI-1; part of #2688; plan §5.1, D27,
// frame B12b).
//
// WHAT EACH CASE HOLDS:
//   * a finding shows what changed, the repair command
//     `swarm install --repair <check>` and the console path where one exists;
//   * the check list shows each check's last result, and "not checked" is
//     drawn as such, never as passing;
//   * no doctor run yet is said, not drawn as healthy.
//
// Fixtures only; no network.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

const RUN_AT = '2026-10-29T14:58:00Z'
const NOW_MS = Date.parse('2026-10-29T15:00:00Z')

function health(over: Json = {}): Json {
  return {
    latest: {
      run_at: RUN_AT,
      checks: [
        { id: 'edge.certificate', title: 'Front door', result: 'passed', detail: 'certificate ACTIVE' },
        { id: 'iap.programmatic_clients', title: 'IAP programmatic clients', result: 'failed', detail: '0 of 1 present' },
        { id: 'scheduler.targets', title: 'Scheduler ticks', result: 'not_checked' },
      ],
      findings: [
        {
          check: 'iap.programmatic_clients',
          title: 'The Desktop OAuth client is no longer on IAP’s programmatic allowlist',
          cause: 'Every new sc login answers 401 with code 900.',
          changed: 'removed 3 h ago by ravi@example.com (Cloud Audit Logs)',
          console_path: 'Security › Identity-Aware Proxy › Settings › Programmatic clients',
        },
      ],
      ...over,
    },
  }
}

async function mount(body: Json) {
  serve((m, url) => (m === 'GET' && url === '/v1/admin/setup/health' ? { status: 200, body } : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { AdminSetupHealth } = await import('../AdminSetupHealth')
  render(<AdminSetupHealth now={NOW_MS} />)
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
})

describe('Admin › Setup › Health', () => {
  it('a finding shows the repair command and the console path', async () => {
    await mount(health())
    const finding = await screen.findByRole('region', { name: /no longer on IAP/ })
    // MUTATION: drop the repair command, or build it from anything but the finding's check.
    expect(visible(finding.querySelector('figure.c-code pre'))).toContain('swarm install --repair iap.programmatic_clients')
    // MUTATION: drop the console path.
    expect(visible(finding)).toContain('Security › Identity-Aware Proxy › Settings › Programmatic clients')
    // MUTATION: drop what changed (the audit-log entry).
    expect(visible(finding)).toContain('removed 3 h ago by ravi@example.com')
  })

  it('a finding with no console path says so instead of inventing one', async () => {
    await mount(health({ findings: [{ check: 'images.digests', title: 'Two services drifted off their digest' }] }))
    const finding = await screen.findByRole('region', { name: /drifted off their digest/ })
    expect(visible(finding)).toContain('swarm install --repair images.digests')
    expect(visible(finding)).toContain('No console path')
  })

  it('draws "not checked" as such, never as passing', async () => {
    await mount(health())
    const list = await screen.findByRole('list', { name: 'Every check' })
    const row = Array.from(list.querySelectorAll('li')).find((li) => visible(li).includes('Scheduler ticks')) as HTMLElement
    // MUTATION: default a missing result to passed.
    expect(visible(row)).toContain('not checked')
    expect(row.className).not.toContain('is-done')
    expect(visible(document.body)).toContain('1 of 3 not checked')
  })

  it('says no doctor run has been recorded yet, not that everything is healthy', async () => {
    await mount({ latest: null })
    expect(await screen.findByText(/No doctor run has been recorded yet/)).toBeTruthy()
    expect(visible(document.body)).not.toMatch(/all (checks )?pass/i)
  })
})
