/**
 * A NAME ON ONE ROW (owner, 2026-11-07, part of #2688). A name is capped at
 * 30 characters at submission, measured at 1440 pixels on the Workflows
 * list's name column (34 fit). A name submitted before the cap still renders
 * on one row: `OneRowName` cuts it with an ellipsis and keeps the whole name
 * in its tooltip, in the Workflows list's name cell and the Agents list's.
 */
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { OneRowName } from '../OneRowName'

const LONG = 'Check the five contract invariants, then write one synthesis of what they found and what is left'

describe('OneRowName', () => {
  it('draws the whole name on one row, cut with an ellipsis, the whole name its tooltip', () => {
    const { container } = render(<OneRowName name={LONG} />)
    const el = container.querySelector<HTMLElement>('.one-row-name')!
    expect(el.textContent).toBe(LONG)
    expect(el.title).toBe(LONG)
    expect(el.style.whiteSpace).toBe('nowrap')
    expect(el.style.overflow).toBe('hidden')
    expect(el.style.textOverflow).toBe('ellipsis')
    expect(el.style.maxWidth).toBe('100%')
  })

  it('takes a tooltip that says more than the name', () => {
    const { container } = render(<OneRowName name="Size chart" title="Size chart · wf_1" />)
    expect(container.querySelector<HTMLElement>('.one-row-name')!.title).toBe('Size chart · wf_1')
  })
})

describe('the lists draw their names through it', () => {
  it('the Workflows list and the Agents list', async () => {
    const fs = await import('node:fs')
    const path = await import('node:path')
    const src = (f: string) => fs.readFileSync(path.resolve(__dirname, '..', f), 'utf8')
    // MUTATION: draw `{named.name}` bare in the `wfg-name` cell and a long name wraps onto a second row.
    expect(src('Workflows.tsx')).toMatch(/<td className="wfg-name"[\s\S]{0,700}<OneRowName name=\{named\.name\}/)
    // MUTATION: draw `{loneTitle}` bare and the Agents row's name is not the shared one-row name.
    expect(src('Agents.tsx')).toMatch(/<OneRowName name=\{loneTitle\}/)
  })
})
