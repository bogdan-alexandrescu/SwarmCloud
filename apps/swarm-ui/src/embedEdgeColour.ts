// TELLING A HUB THAT FRAMES THE CONSOLE THE COLOUR OF THE CONSOLE'S LEFT EDGE.
//
// An app hub can show the console in a frame and draw its own dock and strip
// over the console's left edge. The hub cannot look into a cross-origin frame,
// so it cannot see whether that edge is a light page or a dark sidebar, and a
// dock in the wrong tone disappears into it. The console reads the colour at
// its left edge, halfway down, and posts it as `{ type: 'hub:edge', rgb }`;
// the hub takes the opposite tone.
//
// WHAT IS READ. The elements under a point just inside the left edge, topmost
// first: the first whose background gradient's first colour, or failing that
// its background colour, is at least half opaque (a background image paints
// above the element's background colour). A see-through overlay is
// looked past. When nothing there has one, the page's own background (body,
// then html). A colour the browser serialises as something other than
// `rgb()`/`rgba()` (oklch, a hex in a gradient) is painted on a 1x1 canvas and
// read back as sRGB.
//
// WHEN. Once on start, then again after the DOM changes (a theme switch, a
// page with another layout), the colour scheme changes or the window resizes,
// and after any transition ends, anything scrolls or anything loads, none of
// which need change the DOM: a colour read mid-transition is corrected once it
// ends, a scrolled container can bring another background under the edge, and
// a late stylesheet repaints. Those three are heard on the document in the
// capture phase, since they do not all bubble. Each is read SETTLE_MS after the first of a burst. The wait is not restarted by
// later changes, so a page that never stops changing is still read. Posted
// only when the colour differs from the last one posted.
//
// WHO HEARS IT. The same page and only the same page the ready message goes to
// (embedHandshake.ts): the framing page on the console's scheme under its
// parent domain, never `'*'`. In its own tab, or under any other page, the
// console posts nothing and watches nothing. It listens for nothing back.

import { embedderOrigin, type EmbedWindow } from './embedHandshake'

/** The type of the message the console posts to the hub framing it when its left edge changes colour. */
export const EMBED_EDGE_MESSAGE_TYPE = 'hub:edge'

/** How far in from the left edge the colour is read: under the hub's strip and handle. */
const EDGE_X = 4
/** DOM changes come in bursts; the edge is read this long after the first change of one. */
const SETTLE_MS = 200
/** Events that can repaint the edge without a DOM change, heard on the document in the capture phase. */
const REPAINT_EVENTS = ['transitionend', 'scroll', 'load'] as const
const REPAINT_LISTENER = { capture: true, passive: true } as const
/** A background at least this opaque counts as the edge's colour. */
const OPAQUE = 0.5

type Rgb = [number, number, number]
type Rgba = [number, number, number, number]

/** The parts of `window` this module reads, so a test can hand it one. */
export interface EdgeWindow extends EmbedWindow {
  top: unknown
  innerHeight: number
  document: EmbedWindow['document'] &
    Pick<
      Document,
      'body' | 'documentElement' | 'createElement' | 'elementsFromPoint' | 'addEventListener' | 'removeEventListener'
    >
  getComputedStyle(element: Element): CSSStyleDeclaration
  /** Absent in jsdom. */
  matchMedia?: (query: string) => MediaQueryList
  MutationObserver: typeof MutationObserver
  addEventListener(type: string, listener: () => void): void
  removeEventListener(type: string, listener: () => void): void
}

const RGB_FUNCTION = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:\s*[,/]\s*([\d.]+)(%?))?\s*\)$/
const FIRST_COLOR = /(?:rgba?|hsla?|oklch|oklab|lch|lab|color)\([^)]*\)|#[\da-f]{3,8}\b/i

let canvas: CanvasRenderingContext2D | null | undefined

/** Paints `color` on a 1x1 canvas, which takes any colour the browser knows, and reads it back as sRGB. */
function paint(doc: EdgeWindow['document'], color: string): Rgba | undefined {
  canvas ??= doc.createElement('canvas').getContext('2d', { willReadFrequently: true })
  if (!canvas) return undefined
  canvas.clearRect(0, 0, 1, 1)
  canvas.fillStyle = 'transparent'
  canvas.fillStyle = color
  canvas.fillRect(0, 0, 1, 1)
  const [r = 0, g = 0, b = 0, a = 0] = canvas.getImageData(0, 0, 1, 1).data
  return [r, g, b, a / 255]
}

function toRgba(doc: EdgeWindow['document'], color: string): Rgba | undefined {
  const match = RGB_FUNCTION.exec(color.trim())
  if (!match) return paint(doc, color)
  const [, r, g, b, alpha, percent] = match
  const a = alpha === undefined ? 1 : Number(alpha) / (percent ? 100 : 1)
  return [Number(r), Number(g), Number(b), a]
}

/** The element's background as rgb: its gradient's first colour, else its colour; undefined when see-through. */
function backgroundOf(win: EdgeWindow, element: Element): Rgb | undefined {
  const style = win.getComputedStyle(element)
  const gradient = FIRST_COLOR.exec(style.backgroundImage)?.[0]
  for (const color of [gradient, style.backgroundColor]) {
    const rgba = color ? toRgba(win.document, color) : undefined
    if (rgba && rgba[3] >= OPAQUE) return [Math.round(rgba[0]), Math.round(rgba[1]), Math.round(rgba[2])]
  }
  return undefined
}

/** The colour of the console's left edge, halfway down: the topmost opaque background there. */
function readEdgeColour(win: EdgeWindow): Rgb | undefined {
  const doc = win.document
  const atEdge = typeof doc.elementsFromPoint === 'function' ? doc.elementsFromPoint(EDGE_X, win.innerHeight / 2) : []
  for (const element of [...atEdge, doc.body, doc.documentElement]) {
    const rgb = element ? backgroundOf(win, element) : undefined
    if (rgb) return rgb
  }
  return undefined
}

/**
 * Inside a frame whose page is under the console's parent domain, posts the
 * left edge's colour to that page on start and again whenever it changes.
 * Returns what stops the watch. Anywhere else it does nothing.
 */
export function watchEmbedderEdgeColour(win: EdgeWindow): () => void {
  if (win.self === win.top) return () => {}
  const origin = embedderOrigin(win)
  if (origin === null) return () => {}

  let last = ''
  let timer: ReturnType<typeof setTimeout> | undefined
  const report = () => {
    const rgb = readEdgeColour(win)
    if (!rgb || rgb.join() === last) return
    last = rgb.join()
    win.parent.postMessage({ type: EMBED_EDGE_MESSAGE_TYPE, rgb }, origin)
  }
  const reportSoon = () => {
    timer ??= setTimeout(() => {
      timer = undefined
      report()
    }, SETTLE_MS)
  }

  const observer = new win.MutationObserver(reportSoon)
  observer.observe(win.document.documentElement, {
    attributes: true,
    attributeFilter: ['class', 'style', 'data-theme', 'data-mode'],
    childList: true,
    subtree: true,
  })
  const scheme = typeof win.matchMedia === 'function' ? win.matchMedia('(prefers-color-scheme: dark)') : null
  scheme?.addEventListener('change', reportSoon)
  win.addEventListener('resize', reportSoon)
  for (const type of REPAINT_EVENTS) win.document.addEventListener(type, reportSoon, REPAINT_LISTENER)
  reportSoon()

  return () => {
    clearTimeout(timer)
    observer.disconnect()
    scheme?.removeEventListener('change', reportSoon)
    win.removeEventListener('resize', reportSoon)
    for (const type of REPAINT_EVENTS) win.document.removeEventListener(type, reportSoon, REPAINT_LISTENER)
  }
}
