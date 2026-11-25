// The prose sweep's third number: the visible long runs (proseruns.ts), and
// how proseDelta holds them to the change's base (proseapp.tsx). No table:
// the base is the ceiling, as it is for the words.
import { describe, expect, it } from 'vitest'

import { proseDelta, type Measure } from './proseapp'
import { longRuns, RUN_SENTENCES, RUN_WORDS } from './proseruns'

function runs(html: string): string[] {
  const root = document.createElement('div')
  root.innerHTML = html
  return longRuns(root)
}

const EIGHT = 'one two three four five six seven eight'
const NINE = 'one two three four five six seven eight nine'

describe('longRuns', () => {
  it('draws the line at 9 words and 2 sentences', () => {
    expect(RUN_WORDS).toBe(9)
    expect(RUN_SENTENCES).toBe(2)
  })

  it('passes an 8-word line and returns a 9-word line', () => {
    expect(runs(`<p>${EIGHT}</p>`)).toEqual([])
    expect(runs(`<p>${NINE}</p>`)).toEqual([NINE])
  })

  it('counts only words that contain a letter', () => {
    expect(runs(`<p>${EIGHT} 42 — 7</p>`)).toEqual([])
  })

  it('returns two short sentences', () => {
    expect(runs('<p>Pools are full. Try later.</p>')).toEqual(['Pools are full. Try later.'])
    expect(runs('<p>Pools are full! Try later?</p>')).toEqual(['Pools are full! Try later?'])
  })

  it('does not end a sentence at a decimal point, an abbreviation or an ellipsis', () => {
    expect(runs('<p>Uses 5.6 GiB now.</p>')).toEqual([])
    expect(runs('<p>Pools, e.g. the GPU pool.</p>')).toEqual([])
    expect(runs('<p>Pools, i.e. all of them.</p>')).toEqual([])
    expect(runs('<p>Reading pools...</p>')).toEqual([])
    expect(runs('<p>Reading pools… still.</p>')).toEqual([])
    expect(runs('<p>v1.2.3 is out. Go.</p>')).toEqual([])
  })

  it('does not count a one-word sentence', () => {
    expect(runs('<p>Done. Saved.</p>')).toEqual([])
  })

  it('does not read a tip inside [data-help-description]', () => {
    expect(runs(`<div><span data-help-description>${NINE}</span><p>Pools</p></div>`)).toEqual([])
  })

  it('does not read a hidden node', () => {
    expect(runs(`<div hidden><p>${NINE}</p></div>`)).toEqual([])
    expect(runs(`<p hidden>${NINE}</p>`)).toEqual([])
  })

  it('does not read the rail, the visually hidden words, content or an svg title', () => {
    expect(runs(`<nav class="sk-side"><p>${NINE}</p></nav>`)).toEqual([])
    expect(runs(`<span class="sk-vh">${NINE}</span>`)).toEqual([])
    expect(runs(`<pre data-prose-content>${NINE}</pre>`)).toEqual([])
    expect(runs(`<svg><title>${NINE}</title></svg>`)).toEqual([])
  })

  it('reads a nested <b> as part of its run, not a second run', () => {
    expect(runs('<p>one two <b>three four five</b> six seven eight nine</p>')).toEqual([NINE])
    expect(runs('<p>Pools are <b>full</b>.</p>')).toEqual([])
  })

  it('reads sibling elements as separate runs', () => {
    expect(runs(`<div><span>${EIGHT}</span><span>${EIGHT}</span></div>`)).toEqual([])
  })

  it('judges each fact of a middot list alone', () => {
    expect(runs('<p>3 pools · 2 full · 1 draining · 12 agents running now</p>')).toEqual([])
    expect(runs(`<p>3 pools · ${NINE} · 1 draining</p>`)).toEqual([NINE])
  })

  it('returns the long facts in document order', () => {
    expect(runs(`<div><p>${NINE}</p><p>short</p><p>A b. C d.</p></div>`)).toEqual([NINE, 'A b. C d.'])
  })
})

function m(runList: string[]): Measure {
  return { words: 10, arrows: 0, tokens: [], arrowLinks: [], runs: runList }
}

describe('proseDelta on long runs', () => {
  const A = 'the pool is full so new agents wait in the queue'
  const B = 'a second long sentence that explains what the page does'

  it('passes equal counts', () => {
    expect(proseDelta('capacity/pools', m([A]), m([B]))).toEqual([])
  })

  it('fails one more and names it', () => {
    const out = proseDelta('capacity/pools', m([A]), m([A, B]))
    expect(out).toHaveLength(1)
    expect(out[0]).toContain('capacity/pools')
    expect(out[0]).toContain(JSON.stringify(B))
    expect(out[0]).not.toContain(JSON.stringify(A))
  })

  it('names the repeat when a run is shown twice', () => {
    const out = proseDelta('capacity/pools', m([A]), m([A, A]))
    expect(out).toHaveLength(1)
    expect(out[0]).toContain(JSON.stringify(A))
  })

  it('passes fewer', () => {
    expect(proseDelta('capacity/pools', m([A, B]), m([A]))).toEqual([])
  })

  it('passes a page the base did not have', () => {
    expect(proseDelta('capacity/pools', null, m([A, B]))).toEqual([])
  })
})
