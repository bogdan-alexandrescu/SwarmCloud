// ADMIN › SETUP › HEALTH (DAYONE-SETUP-UI-1, DAYONE-HEALTH-SHAPE; part of
// #2688; plan §5.1, D27, frame B12b).
//
// The page reads what swarm-doctor writes: `scheduled_doctor.report()` puts
// `{run_at, checks, findings}` where every row is `doctor.Finding.to_json()`,
// `{id, status: met|unmet|unknown, detail, step, fix, cause, repair}`, and
// `findings` repeats each row that is not met. Audit 4f found the page read
// its own shape instead (`result: passed|failed`, findings keyed by `check`),
// so every real check drew "not checked" and every finding was dropped; a
// fixture written in the console's shape hid it.
//
// WHAT EACH CASE HOLDS:
//   * the rows here and the fixture build's rows carry exactly the keys
//     doctor.py's Finding.to_json writes, and statuses exitcodes.py defines
//     (read from the Python source, so a writer change turns this red);
//   * met draws passed, unmet draws failed, unknown draws not checked;
//   * an unmet finding shows what the probe saw, the doctor's fix and its
//     repair command; an unknown one says it could not be checked and why;
//   * a finding with no repair (no install step makes it) says so rather
//     than inventing a command;
//   * a row in the console's own shape is NOT read as a pass or a finding;
//   * no doctor run says the hourly doctor has not run yet, never that it
//     is off (every install turns it on: tfvars.py's SCHEDULED_DOCTOR_SHIPPED,
//     read from the Python source), and is not drawn as healthy;
//   * NO EXPLANATORY PROSE ON THE SURFACE (the owner's standing rule, 2026-11-05):
//     why a check could not look is the tooltip of a tick beside its title,
//     "no repair command" is the title's tooltip, and what Health is sits
//     behind an (i) beside the heading; each fact is still in the page.
//
// Fixtures only; no network.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

const RUN_AT = '2026-10-29T14:58:00Z'
const NOW_MS = Date.parse('2026-10-29T15:00:00Z')
const INSTALL = join(__dirname, '..', '..', '..', 'swarm-install', 'swarm_install')

/** One row as `doctor.Finding.to_json()` writes it. */
function row(id: string, status: string, over: Json = {}): Json {
  const step = over.step === undefined ? 'foundations.apis' : over.step
  return {
    id,
    status,
    detail: `${id}: what the probe saw`,
    step,
    fix: step === null ? `Fix: no install step makes this; see the entry's check: ${id}.` : `Fix: the install's step ${step} makes it: swarm install --repair ${id}.`,
    cause: '',
    repair: step === null ? null : `swarm install --repair ${id}`,
    ...over,
  }
}

/** A run as `scheduled_doctor.report()` writes it: every row a check, the ones not met again as findings. */
function report(rows: Json[]): Json {
  return { latest: { run_at: RUN_AT, checks: rows, findings: rows.filter((r) => r.status !== 'met') } }
}

const RUN = [
  row('api.artifactregistry', 'met', { detail: 'artifactregistry.googleapis.com is enabled' }),
  row('api.cloudkms', 'unmet', { detail: 'cloudkms.googleapis.com is not enabled' }),
  row('quota.cloud-run-jobs', 'unknown', { step: null, cause: 'missing_grant', detail: 'missing grant: the doctor lacks serviceusage.quotas.get, so this could not be checked' }),
]

/** The keys `Finding.to_json` returns, read from doctor.py. */
function writerKeys(): string[] {
  const py = readFileSync(join(INSTALL, 'doctor.py'), 'utf8')
  const body = /def to_json\(self\)[^\n]*\n\s+return \{([\s\S]*?)\n\s+\}/.exec(py)
  expect(body, 'doctor.py has no Finding.to_json returning a dict literal').not.toBeNull()
  return Array.from(((body as RegExpExecArray)[1] ?? '').matchAll(/"(\w+)":/g), (m) => m[1] ?? '').sort()
}

/** MET, UNMET and UNKNOWN as exitcodes.py defines them. */
function writerStatuses(): string[] {
  const py = readFileSync(join(INSTALL, 'exitcodes.py'), 'utf8')
  return ['MET', 'UNMET', 'UNKNOWN'].map((name) => {
    const m = new RegExp(`^${name} = "(\\w+)"`, 'm').exec(py)
    expect(m, `exitcodes.py defines no ${name}`).not.toBeNull()
    return (m as RegExpExecArray)[1] ?? ''
  })
}

async function mount(body: Json) {
  serve((m, url) => (m === 'GET' && url === '/v1/admin/setup/health' ? { status: 200, body } : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { AdminSetupHealth } = await import('../AdminSetupHealth')
  render(<AdminSetupHealth now={NOW_MS} />)
}

/** The text a reader sees: tooltips and hidden descriptions left out. */
function shown(el: Element | null): string {
  if (el === null) return ''
  const copy = el.cloneNode(true) as Element
  for (const off of Array.from(copy.querySelectorAll('[role="tooltip"], [hidden]'))) off.remove()
  return visible(copy)
}

/** The tooltip of the host around `el`. */
const tipOf = (el: Element | null) => visible(el?.closest('.c-tip-host')?.querySelector('[role="tooltip"]') ?? null)

const checkRow = (list: HTMLElement, id: string) => Array.from(list.querySelectorAll('li')).find((li) => visible(li).includes(id)) as HTMLElement

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
})

describe('Admin › Setup › Health reads the doctor’s shape', () => {
  it('the rows here and the fixture build’s rows carry exactly the keys and statuses the doctor writes', async () => {
    const keys = writerKeys()
    const statuses = writerStatuses()
    expect(statuses).toEqual(['met', 'unmet', 'unknown'])
    for (const r of RUN) expect(Object.keys(r).sort()).toEqual(keys)
    // MUTATION: write the fixture in the console's own shape (`result`, `check`, `title`).
    const { fixtureSetupHealth, normSetupHealth } = await import('../api')
    const latest = fixtureSetupHealth().latest as { checks: Json[]; findings: Json[] }
    expect(latest.checks.length).toBeGreaterThan(0)
    for (const r of [...latest.checks, ...latest.findings]) {
      expect(Object.keys(r).sort()).toEqual(keys)
      expect(statuses).toContain(r.status)
    }
    expect(latest.findings).toEqual(latest.checks.filter((r) => r.status !== 'met'))
    // The fixture build draws every result, and keeps every finding.
    const seen = normSetupHealth(fixtureSetupHealth())
    expect(new Set(seen?.checks.map((c) => c.result))).toEqual(new Set(['passed', 'failed', 'not_checked']))
    expect(seen?.findings.length).toBe(latest.findings.length)
  })

  it('draws met as passed, unmet as failed and unknown as not checked', async () => {
    await mount(report(RUN))
    const list = await screen.findByRole('list', { name: 'Every check' })
    // MUTATION: read `result`/`state` instead of the doctor's `status`.
    expect(visible(checkRow(list, 'api.artifactregistry'))).toContain('passed')
    expect(checkRow(list, 'api.artifactregistry').className).toContain('is-done')
    expect(visible(checkRow(list, 'api.cloudkms'))).toContain('failed')
    expect(visible(checkRow(list, 'api.cloudkms'))).toContain('cloudkms.googleapis.com is not enabled')
    expect(visible(checkRow(list, 'quota.cloud-run-jobs'))).toContain('not checked')
    expect(checkRow(list, 'quota.cloud-run-jobs').className).not.toContain('is-done')
    expect(visible(document.body)).toContain('1 of 3 not checked')
  })

  it('an unmet finding shows what the probe saw, the doctor’s fix and its repair command', async () => {
    await mount(report(RUN))
    // MUTATION: keep a finding only when it has `check` (the doctor writes `id`).
    const finding = await screen.findByRole('region', { name: 'api.cloudkms' })
    expect(visible(finding)).toContain('api.cloudkms drifted')
    expect(visible(finding)).toContain('cloudkms.googleapis.com is not enabled')
    // MUTATION: drop the doctor's fix.
    expect(visible(finding)).toContain("the install's step foundations.apis makes it")
    // MUTATION: drop the doctor's repair, or draw anything but its own.
    expect(visible(finding.querySelector('figure.c-code pre'))).toMatch(/(^|\d)swarm install --repair api\.cloudkms$/)
    expect(visible(document.body)).toContain('1 drifted')
  })

  it('an unknown finding says it could not be checked and why, and invents no repair', async () => {
    await mount(report(RUN))
    const finding = await screen.findByRole('region', { name: 'quota.cloud-run-jobs' })
    // MUTATION: draw an unknown finding as drift.
    expect(visible(finding)).toContain('could not be checked')
    expect(visible(finding)).not.toContain('drifted')
    // MUTATION: drop the cause.
    expect(visible(finding)).toContain('lacks a permission this check needs')
    // MUTATION: invent a command when the doctor names none.
    expect(finding.querySelector('figure.c-code')).toBeNull()
    expect(visible(finding)).toContain('there is no repair command')
    expect(visible(finding)).toContain('no install step makes this')
    expect(visible(document.body)).toContain('1 could not be checked')
  })

  it('does not read a row in the console’s own shape as a pass or a finding', async () => {
    const consoleShaped = {
      latest: {
        run_at: RUN_AT,
        checks: [{ id: 'edge.certificate', title: 'Front door', result: 'passed' }],
        findings: [{ check: 'iap.programmatic_clients', title: 'Dropped from the allowlist', repair: 'gcloud iap settings set' }],
      },
    }
    await mount(consoleShaped)
    const list = await screen.findByRole('list', { name: 'Every check' })
    // MUTATION: accept `result: passed` (the doctor never writes it).
    expect(visible(checkRow(list, 'edge.certificate'))).toContain('not checked')
    expect(screen.queryByRole('region')).toBeNull()
    expect(visible(document.body)).toContain('no findings')
  })

  it('says the hourly doctor has not run yet when no run is recorded, not that it is off or that everything is healthy', async () => {
    // Every install turns the doctor on in infra pass 2; if that stops, this wording is wrong.
    expect(readFileSync(join(INSTALL, 'tfvars.py'), 'utf8')).toMatch(/^SCHEDULED_DOCTOR_SHIPPED = True$/m)
    await mount({ latest: null })
    // MUTATION: say the doctor is off right after an install that turned it on.
    expect(await screen.findByText(/The hourly doctor has not run yet/)).toBeTruthy()
    expect(visible(document.body)).not.toMatch(/doctor is off/)
    // Why nothing is known to pass is the (i)'s tooltip, not a line on the page.
    expect(tipOf(screen.getByRole('button', { name: 'About the doctor run' }))).toMatch(/nothing here is known to pass/)
    expect(visible(document.body)).not.toMatch(/all (checks )?pass/i)
    expect(visible(document.body)).not.toContain('runs every hour')
  })

  it('keeps the explanations behind tooltips, every fact still in the page', async () => {
    await mount(report(RUN))
    const finding = await screen.findByRole('region', { name: 'quota.cloud-run-jobs' })
    // The must-stay status stays on the surface.
    expect(shown(finding)).toContain('quota.cloud-run-jobs could not be checked')
    expect(shown(document.body)).toContain('1 could not be checked')
    // MUTATION: print the cause as a "Why:" line again.
    expect(shown(finding)).not.toContain('lacks a permission')
    expect(shown(finding)).not.toContain('Why:')
    const tick = finding.querySelector('.asm-tick')
    expect(tick?.getAttribute('aria-label')).toContain('lacks a permission this check needs')
    expect(tipOf(tick)).toContain('lacks a permission this check needs')
    // MUTATION: print "No install step makes this one" under the finding again.
    expect(shown(finding)).not.toContain('no repair command')
    expect(tipOf(finding.querySelector('.asm-explained'))).toContain('there is no repair command')
    // A finding with a repair command has no such tooltip.
    expect((await screen.findByRole('region', { name: 'api.cloudkms' })).querySelector('.asm-explained')).toBeNull()
    // MUTATION: keep the doctor's description in the card's foot.
    expect(shown(document.body)).not.toContain('The checks the hourly doctor runs')
    expect(shown(document.body)).toContain('Last run 2 min ago')
    const about = screen.getByRole('button', { name: 'About Health' })
    expect(tipOf(about)).toBe('The checks the hourly doctor runs, read-only, on the live deployment.')
  })
})

// THE PAGE'S (i) BESIDE ITS TITLE (the owner's pick, 2026-11-05): what Health
// is, and where the install steps are, was a paragraph under the head; it is
// the (i) in PageHead's info slot, beside the "Health" title, word for word.
describe('Admin › Setup › Health, the page’s (i)', () => {
  it('sits beside the title, in the page head’s info slot, and the help line under the head is gone', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/admin/setup/health' ? { status: 200, body: report(RUN) } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { AdminSetupHealthScreen } = await import('../AdminSetup')
    render(<AdminSetupHealthScreen now={NOW_MS} />)
    await screen.findByRole('region', { name: 'quota.cloud-run-jobs' })
    const head = document.querySelector('.c-phead .head') as HTMLElement
    // MUTATION: draw the page's explanation as a paragraph under the head again -> fails.
    const about = head.querySelector('button[aria-label="About the Health page"]')
    expect(about).not.toBeNull()
    expect(about?.closest('.c-tip-host')?.previousElementSibling?.tagName).toBe('H1')
    expect(tipOf(about)).toBe(
      'The hourly doctor’s checks, read-only, on the live deployment: the scheduled run’s findings, each with its repair. From a terminal, swarm doctor checks this machine and the deployment against the manifest. The install steps are on Admin › Setup.',
    )
    expect(document.querySelector('.asm-page > .ur-sub')).toBeNull()
    expect(shown(document.body)).not.toContain('swarm doctor checks this machine')
  })
})
