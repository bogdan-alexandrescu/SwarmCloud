// EVERY SHIPPED STYLESHEET, for the source-scan gates (typescale,
// letter-spacing, the stylesheet gate). `styles.css` is the sheet main.tsx
// imports; `src/styles/*.css` are the per-area sheets a component imports --
// the canonical components' first (components.html A, 2026-10-02), then each
// screen lane's own. A gate that read only `styles.css` would wave a new
// sheet through unread, which is how the 23 inline sizes typescale.test.ts
// names survived the first pass.
//
// `?raw` through Vite's own loader, as every gate here reads its input, so a
// moved file fails to resolve instead of quietly reading as empty.
import STYLES from '../styles.css?raw'

const AREA = import.meta.glob('../styles/*.css', { query: '?raw', import: 'default', eager: true }) as Record<string, string>

/** [path relative to the app, text], `styles.css` first. */
export const SHEETS: ReadonlyArray<readonly [string, string]> = [
  ['src/styles.css', STYLES],
  ...Object.entries(AREA)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([path, text]) => [`src/styles/${path.split('/').pop()}`, text] as const),
]

/** Every sheet's text, one after another, for a scan that only needs to find a rule. */
export const ALL_CSS: string = SHEETS.map(([, text]) => text).join('\n')
