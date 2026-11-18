// THE FACE THE CASCADE GIVES AN ELEMENT: mono or sans (visual QA Q1,
// 2026-10-02: mono is for ids, values, code and timestamps only).
//
// The nearest `font-family` or `font` declaration on the element or an
// ancestor, through `marks.painted` (the shipped sheets, the real cascade):
// the cascade does not model inheritance, so this walks up, and `inherit`
// keeps walking.
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

export function familyOf(el: Element, env: CascadeEnv): 'mono' | 'sans' {
  for (let node: Element | null = el; node !== null; node = node.parentElement) {
    const v = painted(node, ['font-family', 'font'], env)
    if (v === null || /^\s*inherit\s*$/.test(v)) continue
    return /--mono\b|monospace|DM Mono/.test(v) ? 'mono' : 'sans'
  }
  return 'sans'
}
