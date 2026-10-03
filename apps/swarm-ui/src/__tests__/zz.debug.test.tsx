import { render, waitFor } from '@testing-library/react'
import { it } from 'vitest'
import { App } from '../App'
import SHEET from '../styles.css?raw'
import { cascade } from './cssgate'
const STYLES = [SHEET, ...Object.values(import.meta.glob<string>('../styles/*.css', { query: '?raw', import: 'default', eager: true }))].join('\n')
function why(el: Element): string {
  for (let n: Element | null = el; n; n = n.parentElement) {
    const r = cascade(STYLES, n, ['font-family', 'font'], { width: 1440 })
    if (r.winner && !/^\s*inherit\s*$/.test(r.winner.value)) return `${n.tagName}.${n.className} <= ${r.winner.selector} {${r.winner.property}: ${r.winner.value}}`
  }
  return 'none'
}
it('debug', async () => {
  const out = new Set<string>()
  for (const p of (process.env.DBG_PATHS ?? '/capacity/pools').split(',')) {
    window.history.replaceState(null, '', p)
    const { unmount } = render(<App />)
    await waitFor(() => { if (!document.querySelector(process.env.DBG_WAIT ?? 'main.work th')) throw new Error('x') }, { timeout: 6000 }).catch(() => out.add(p + ' NO-WAIT-MATCH'))
    out.add(p + ' examined ' + document.querySelectorAll(process.env.DBG_SEL ?? 'main.work th').length)
    for (const el of document.querySelectorAll(process.env.DBG_SEL ?? 'main.work th')) { const w = why(el); if (/mono/.test(w)) out.add(p + ' ' + w) }
    unmount()
  }
  console.log([...out].join('\n'))
})
