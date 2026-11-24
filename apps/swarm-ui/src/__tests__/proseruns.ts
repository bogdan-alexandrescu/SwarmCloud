// THE PROSE SWEEP'S THIRD NUMBER: the visible long runs of a page.
//
// The word count (proseapp.tsx) says a page got wordier; it cannot say
// whether those words are a table of facts or a paragraph of explanation.
// The rule (owner, 2026-11-05) forbids the paragraph, not the table, so this
// reads what a paragraph looks like on screen: one element's run of text
// that is long (RUN_WORDS words or more) or that says two things in a row
// (RUN_SENTENCES sentences or more). A fact list (`3 pools · 2 full`) is cut
// at its middots and each fact judged alone, so a dense status line passes
// while one long fact inside it does not.
//
// It reads what measure() reads: the same nodes are taken out (help copy,
// content the page shows rather than says, the rail, visually hidden words,
// styles and scripts), and also hidden nodes and an svg's <title>, which no
// reader sees on the surface.

/** A fact of this many words (each containing a letter) or more is a long run. */
export const RUN_WORDS = 9
/** A fact of this many sentences or more is a long run. */
export const RUN_SENTENCES = 2

const UNREAD = '[data-help-description], [data-prose-content], .sk-side, .sk-vh, style, script, [hidden], svg title'

const LETTER = /\p{L}/u
/** A token that ends a sentence: a final . ! or ?, optionally inside a closing quote or bracket. */
const END = /[.!?]["'’”)\]]*$/
/** An ellipsis is a pause, not an end. */
const ELLIPSIS = /(?:\.\.\.|…)["'’”)\]]*$/
/** `e.g.`, `i.e.`, `U.S.`: letters each followed by a dot. */
const DOTTED = /^\(?(?:\p{L}\.){2,}$/u
/** Abbreviations that end with one dot and rarely end a sentence. */
const ABBREVIATIONS = new Set(['etc.', 'vs.', 'cf.', 'approx.', 'incl.', 'min.', 'max.', 'no.'])

function words(tokens: readonly string[]): number {
  return tokens.filter((t) => LETTER.test(t)).length
}

function endsSentence(token: string): boolean {
  if (!END.test(token) || ELLIPSIS.test(token)) return false
  if (DOTTED.test(token)) return false
  return !ABBREVIATIONS.has(token.toLowerCase().replace(/^\(/, ''))
}

/** The sentences of `fact`: each ends at . ! or ? before whitespace or the end, and has two words or more. */
export function sentences(fact: string): number {
  let count = 0
  let current: string[] = []
  for (const token of fact.split(/\s+/).filter(Boolean)) {
    current.push(token)
    if (endsSentence(token)) {
      if (words(current) >= 2) count += 1
      current = []
    }
  }
  return count
}

/** Whether one fact is long: RUN_WORDS words or more, or RUN_SENTENCES sentences or more. */
export function isLong(fact: string): boolean {
  return words(fact.split(/\s+/).filter(Boolean)) >= RUN_WORDS || sentences(fact) >= RUN_SENTENCES
}

function hasOwnText(el: Element): boolean {
  return [...el.childNodes].some((n) => n.nodeType === 3 && (n.textContent ?? '').trim() !== '')
}

/**
 * The text runs of `el` in document order: the whitespace-collapsed text of
 * each OUTERMOST element that has a non-blank text node of its own, so
 * `<p>a <b>b</b> c</p>` is one run and its `<b>` is not a second.
 */
function textRuns(el: Element, out: string[]): void {
  if (hasOwnText(el)) {
    const text = (el.textContent ?? '').replace(/\s+/g, ' ').trim()
    if (text !== '') out.push(text)
    return
  }
  for (const child of el.children) textRuns(child, out)
}

/** The long facts `root` shows, in document order. */
export function longRuns(root: HTMLElement): string[] {
  const clone = root.cloneNode(true) as HTMLElement
  for (const el of [...clone.querySelectorAll(UNREAD)]) el.remove()
  if (clone.matches(UNREAD)) return []
  const runs: string[] = []
  textRuns(clone, runs)
  return runs
    .flatMap((run) => run.split(/\s·\s/))
    .map((fact) => fact.trim())
    .filter(isLong)
}

/**
 * The help copy inside `root` (issue 81): the whitespace-collapsed text of
 * each `[data-help-description]` node in it -- an (i)'s tip, a rule's fold --
 * joined by a space; '' when there is none. A test asserting a moved sentence
 * reads it here, at its new place.
 */
export function helpIn(root: Element | null | undefined): string {
  if (root === null || root === undefined) return ''
  return [...root.querySelectorAll('[data-help-description]')]
    .map((el) => (el.textContent ?? '').replace(/\s+/g, ' ').trim())
    .join(' ')
}

/** The text of `root` a reader sees: `root` with its help copy taken out, whitespace collapsed. */
export function shownIn(root: Element | null | undefined): string {
  if (root === null || root === undefined) return ''
  const clone = root.cloneNode(true) as Element
  for (const el of [...clone.querySelectorAll('[data-help-description]')]) el.remove()
  return (clone.textContent ?? '').replace(/\s+/g, ' ').trim()
}

/** The first element under `root` matching `selector`, or null. */
export const oneIn = <T extends Element = HTMLElement>(root: ParentNode, selector: string): T | null => root.querySelector<T>(selector)

/** Every element under `root` matching `selector`. */
export const allIn = <T extends Element = HTMLElement>(root: ParentNode, selector: string): T[] => [...root.querySelectorAll<T>(selector)]

/** The nearest element around `el` matching `selector`, or null. */
export const around = <T extends Element = HTMLElement>(el: Element, selector: string): T | null => el.closest<T>(selector)
