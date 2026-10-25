// AN ADMIN PAGE'S LEAD, MEASURED THE WAY THE SPACING SWEEP MEASURES IT
// (ADMIN-LEADS; part of #2157).
//
// spacing.admin.test.tsx found the Tenants lead's "Open its progress" with a
// control boundary of 2.89:1 on the lead's info tint: jsdom resolves the
// cascade by source order alone, every section sheet loads before
// styles.css, and so `:where(.app button) { border: 1px solid var(--ctl-bd) }`
// out-ranked the primary's own accent edge there, though no browser draws it.
// styles.css restates the accent after the floor (`.adm-lead .c-btn.is-primary`).
// This file holds that for every tone that carries a button, in both themes,
// with the sweep's own `probe` over the sheet the sweep resolves, in well
// under the sweep's minutes. Fixtures only; nothing here is token-shaped.

import STYLES from '../styles.css?raw'
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import { AdminLead, type AdminLeadTone } from '../AdminSettings'
import { Button } from '../components'
import { probe, resolveSheet, type Finding } from './spaceprobe'

type Theme = 'dark' | 'light'

const SECTION_SHEETS = Object.values(
  import.meta.glob<string>('../styles/*.css', { query: '?raw', import: 'default', eager: true }),
)
const SHEET = [...SECTION_SHEETS, STYLES].join('\n')
const RESOLVED: Record<Theme, string> = { dark: resolveSheet(SHEET, 'dark'), light: resolveSheet(SHEET, 'light') }

function measure(root: HTMLElement, theme: Theme): Finding[] {
  const style = document.createElement('style')
  style.textContent = RESOLVED[theme]
  document.head.appendChild(style)
  try {
    return probe(root).findings
  } finally {
    style.remove()
  }
}

const said = (fs: Finding[]) => fs.map((f) => `${f.kind} ${f.where} [${f.side}] ${f.measured} < ${f.floor} — ${f.detail}`)

afterEach(cleanup)

// Inside `.app`, where the console draws it: the `:where(.app button)` floor
// reaches only a button inside `.app`.
function lead(tone: AdminLeadTone): HTMLElement {
  const { container } = render(
    <div className="app">
      <AdminLead tone={tone} kicker="Kicker" title="One sentence" body="Its detail." action={<Button kind="primary">Open it</Button>} />
    </div>,
  )
  return container
}

describe('the lead’s one button clears 3:1 on every tint', () => {
  for (const tone of ['next', 'warn'] as const) {
    for (const theme of ['dark', 'light'] as const) {
      it(`${tone} lead, ${theme}`, () => {
        const findings = measure(lead(tone), theme).filter((f) => f.kind === 'divider-under-floor' && f.where.startsWith('button.c-btn'))
        // MUTATION: delete `.adm-lead .c-btn.is-primary` from styles.css and the
        // next lead reads `--ctl-bd` on `--sk-acs` at 2.89:1 dark, and fails.
        expect(said(findings)).toEqual([])
      })
    }
  }
})
