/**
 * THE USER GUIDE (`/guide`, `/guide/<page>`): day-one onboarding, the
 * engineer's quick start, everyday work and troubleshooting, for a person who
 * has never seen SwarmCloud.
 *
 * The source is docs/user-guide/*.md; `guidePages.ts` is its generated copy
 * (`guide-sync.mjs` says why it is a copy). The pages are drawn here by a small
 * Markdown reader -- headings, paragraphs, lists, tables, fenced code, links,
 * bold and code spans, the only things the guide writes -- into React
 * elements, so no HTML string is ever injected and no dependency is added.
 *
 * Not a section and not a tab, like the join page: Help and the setup
 * checklist link here, and it sits behind the console's own sign-in. The
 * console's no-prose rule is for console pages; these pages are the prose.
 *
 * REAL VALUES WITHOUT REAL VALUES IN THE REPOSITORY. A page may write three
 * tokens -- `{{console}}`, `{{plugin-home}}` (owner/name), `{{plugin-version}}`
 * -- and they are filled here, at render time: the console address from the
 * page's own origin; the plugin's version from the deployment's public
 * discovery document (`/.well-known/swarmcloud`, `plugin.min_version`, the
 * oldest plugin it accepts, whose `sc-v<version>` tag the release pushed), and
 * the plugin's home from the same read (`plugin.home`, a plain `owner/name`).
 * A value nothing served, or served malformed, is the marked placeholder the
 * reader replaces, never an empty string and never the served text:
 * `github.com/@sc-v#` reads as a command and is not one.
 */
import { createContext, useContext, useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from 'react'
import { USE_FIXTURES } from './api'
import { GUIDE_SOURCES } from './guidePages'
import './styles/guide.css'

export const GUIDE_PATH = '/guide'
export const GUIDE_INDEX = 'index'

/** The guide page a pathname names (`''` for the index), or null off the guide. */
export function guideSlugFrom(pathname: string): string | null {
  const p = pathname.replace(/\/+$/, '')
  if (p === GUIDE_PATH) return ''
  if (!p.startsWith(`${GUIDE_PATH}/`)) return null
  const slug = p.slice(GUIDE_PATH.length + 1)
  return slug === GUIDE_INDEX ? '' : slug
}

export function guidePath(slug: string): string {
  return slug === '' || slug === GUIDE_INDEX ? GUIDE_PATH : `${GUIDE_PATH}/${slug}`
}

/** A page's title: its first `# ` heading. */
export function guideTitle(markdown: string): string {
  const m = /^# (.+)$/m.exec(markdown)
  return m?.[1]?.trim() ?? ''
}

export type GuidePage = { slug: string; title: string }

/** Every page, in the index's reading order, the index first; then any page the index does not link. */
export function guidePages(sources: Readonly<Record<string, string>> = GUIDE_SOURCES): GuidePage[] {
  const order: string[] = [GUIDE_INDEX]
  const index = sources[GUIDE_INDEX] ?? ''
  for (const m of index.matchAll(/\]\(([a-z0-9-]+)\.md(?:#[^)]*)?\)/g)) {
    const slug = m[1]
    if (slug !== undefined && slug in sources && !order.includes(slug)) order.push(slug)
  }
  for (const slug of Object.keys(sources).sort()) if (!order.includes(slug)) order.push(slug)
  return order.filter((s) => s in sources).map((slug) => ({ slug, title: guideTitle(sources[slug] ?? '') }))
}

/** The id a heading's text gets, so `page.md#everyday-work` lands on it. */
export function headingId(text: string): string {
  return text
    .toLowerCase()
    .replace(/[`*]/g, '')
    .replace(/[^a-z0-9 -]/g, '')
    .trim()
    .replace(/\s+/g, '-')
}

// ------------------------------------------------------------ the tokens

/** What fills a page's tokens. Absent (or blank) means nothing served it. */
export type GuideValues = { console?: string; pluginHome?: string; pluginVersion?: string }

/** What the deployment serves, beside the origin the page already knows. */
export type GuideServed = Omit<GuideValues, 'console'>

const TOKENS: Readonly<Record<string, { key: keyof GuideValues; placeholder: string }>> = {
  console: { key: 'console', placeholder: '<console-address>' },
  'plugin-home': { key: 'pluginHome', placeholder: '<owner/name>' },
  'plugin-version': { key: 'pluginVersion', placeholder: '<version>' },
}

const TOKEN = /\{\{([a-z-]+)\}\}/g

/** Why a placeholder is drawn, on the placeholder itself. */
const MISSING = 'This deployment does not publish this value: ask your platform team for it and type it here.'

type Filled = string | { placeholder: string }

/** A text's tokens, each filled or, when nothing served it, its placeholder. */
function fillParts(text: string, values: GuideValues): Filled[] {
  const out: Filled[] = []
  let last = 0
  for (const m of text.matchAll(TOKEN)) {
    const token = TOKENS[m[1] ?? '']
    if (token === undefined) continue
    const at = m.index ?? 0
    if (at > last) out.push(text.slice(last, at))
    const value = values[token.key]?.trim() ?? ''
    out.push(value === '' ? { placeholder: token.placeholder } : value)
    last = at + m[0].length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

/** A text with its tokens filled, as Copy hands it over. */
export function fillGuide(text: string, values: GuideValues): string {
  return fillParts(text, values)
    .map((p) => (typeof p === 'string' ? p : p.placeholder))
    .join('')
}

const GuideValuesContext = createContext<GuideValues>({})

/** A text with its tokens filled, a placeholder marked as one. */
function FilledText({ text }: { text: string }) {
  const values = useContext(GuideValuesContext)
  return (
    <>
      {fillParts(text, values).map((p, j) =>
        typeof p === 'string' ? (
          p
        ) : (
          <mark key={j} className="guide-fill-missing" title={MISSING}>
            {p.placeholder}
          </mark>
        ),
      )}
    </>
  )
}

/** Where the deployment publishes the plugin's version: public, beside the console. */
export const DISCOVERY_PATH = '/.well-known/swarmcloud'

/** A plain release version, the shape the `sc-v<version>` tag carries. */
const VERSION = /^\d+\.\d+\.\d+$/

/** A plain `owner/name`: one slash, nothing a URL, a space or markup could carry. */
const HOME = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/

/**
 * What the deployment serves for the tokens. Never rejects: a missing,
 * refused or malformed document serves nothing, and the page draws the
 * placeholders.
 */
export async function loadGuideServed(fetcher: (url: string, init?: RequestInit) => Promise<Response> = (u, i) => fetch(u, i)): Promise<GuideServed> {
  try {
    const res = await fetcher(DISCOVERY_PATH, { headers: { Accept: 'application/json' } })
    if (!res.ok) return {}
    const body: unknown = JSON.parse(await res.text())
    if (typeof body !== 'object' || body === null || Array.isArray(body)) return {}
    const plugin: unknown = (body as { plugin?: unknown }).plugin
    if (typeof plugin !== 'object' || plugin === null) return {}
    const { min_version: version, home } = plugin as { min_version?: unknown; home?: unknown }
    return {
      ...(typeof home === 'string' && HOME.test(home) ? { pluginHome: home } : {}),
      ...(typeof version === 'string' && VERSION.test(version.trim()) ? { pluginVersion: version.trim() } : {}),
    }
  } catch {
    return {}
  }
}

let served: Promise<GuideServed> | null = null

/** The served values, read once per console load; nothing on the fixture build. */
function useGuideServed(given: GuideServed | undefined): GuideServed {
  const [got, setGot] = useState<GuideServed>({})
  const read = given === undefined && !USE_FIXTURES
  useEffect(() => {
    if (!read) return
    let live = true
    served ??= loadGuideServed()
    void served.then((v) => {
      if (live) setGot(v)
    })
    return () => {
      live = false
    }
  }, [read])
  return given ?? got
}

/** How long Copy's "copied ✓" stays. */
const COPY_SAID_MS = 2000

/** A command block, its tokens filled, with a Copy button that copies what it shows. */
function CodeBlock({ text }: { text: string }) {
  const values = useContext(GuideValuesContext)
  const [said, setSaid] = useState('')
  useEffect(() => {
    if (said === '') return
    const id = setTimeout(() => setSaid(''), COPY_SAID_MS)
    return () => clearTimeout(id)
  }, [said])
  const copy = () => {
    const c = typeof navigator === 'undefined' ? undefined : navigator.clipboard
    if (c === undefined) return setSaid('could not copy; select the text instead')
    c.writeText(fillGuide(text, values)).then(
      () => setSaid('copied'),
      () => setSaid('could not copy; select the text instead'),
    )
  }
  return (
    <div className="guide-codebox">
      <pre className="guide-code" tabIndex={0}>
        <code>
          <FilledText text={text} />
        </code>
      </pre>
      <button type="button" className="guide-copy" onClick={copy} aria-label="Copy this command">
        {said === 'copied' ? 'copied ✓' : 'Copy'}
      </button>
      <span className="guide-copy-said" role="status">
        {said === 'copied' ? '' : said}
      </span>
    </div>
  )
}

// ------------------------------------------------------------ the reader

export type Block =
  | { kind: 'heading'; level: number; text: string }
  | { kind: 'para'; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'list'; ordered: boolean; items: Block[][] }
  | { kind: 'table'; head: string[]; rows: string[][] }

const ITEM = /^(\s*)([*-]|\d+\.)\s+(.*)$/

function cells(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((c) => c.trim())
}

/** Markdown to blocks: the subset the guide writes, read the way GitHub reads it. */
export function parseMarkdown(markdown: string): Block[] {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n')
  const blocks: Block[] = []
  let i = 0
  while (i < lines.length) {
    const line = lines[i] ?? ''
    if (line.trim() === '') {
      i++
      continue
    }
    if (/^\s*```/.test(line)) {
      const body: string[] = []
      i++
      while (i < lines.length && !/^\s*```/.test(lines[i] ?? '')) body.push(lines[i++] ?? '')
      i++
      blocks.push({ kind: 'code', text: body.join('\n') })
      continue
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(line)
    if (h) {
      blocks.push({ kind: 'heading', level: h[1]?.length ?? 1, text: h[2] ?? '' })
      i++
      continue
    }
    if (line.trim().startsWith('|') && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1] ?? '')) {
      const head = cells(line)
      const rows: string[][] = []
      i += 2
      while (i < lines.length && (lines[i] ?? '').trim().startsWith('|')) rows.push(cells(lines[i++] ?? ''))
      blocks.push({ kind: 'table', head, rows })
      continue
    }
    const item = ITEM.exec(line)
    if (item) {
      const base = item[1]?.length ?? 0
      const ordered = /\d/.test(item[2] ?? '')
      const items: Block[][] = []
      let current: string[] | null = null
      while (i < lines.length) {
        const l = lines[i] ?? ''
        const m = ITEM.exec(l)
        if (m && (m[1]?.length ?? 0) === base && /\d/.test(m[2] ?? '') === ordered) {
          if (current !== null) items.push(parseMarkdown(current.join('\n')))
          current = [m[3] ?? '']
          i++
          continue
        }
        if (l.trim() === '') {
          // A blank line ends the list unless the next line is still inside it.
          const next = lines[i + 1] ?? ''
          const nm = ITEM.exec(next)
          const indented = /^\s+\S/.test(next) && next.search(/\S/) > base
          if (indented || (nm && (nm[1]?.length ?? 0) === base && /\d/.test(nm[2] ?? '') === ordered)) {
            current?.push('')
            i++
            continue
          }
          break
        }
        if (l.search(/\S/) > base) {
          // A continuation, dedented to the item's own text.
          current?.push(l.replace(new RegExp(`^\\s{0,${base + 3}}`), ''))
          i++
          continue
        }
        break
      }
      if (current !== null) items.push(parseMarkdown(current.join('\n')))
      blocks.push({ kind: 'list', ordered, items })
      continue
    }
    const para: string[] = []
    while (i < lines.length) {
      const l = lines[i] ?? ''
      if (l.trim() === '' || /^(#{1,6})\s/.test(l) || /^\s*```/.test(l) || ITEM.test(l) || (l.trim().startsWith('|') && para.length === 0)) break
      para.push(l.trim())
      i++
    }
    if (para.length === 0) {
      // A line no rule above took (a lone table row): drawn as text, never dropped.
      para.push(line.trim())
      i++
    }
    blocks.push({ kind: 'para', text: para.join(' ') })
  }
  return blocks
}

/** Where a guide link goes: a sibling page to its /guide path, a console path as written. */
export function guideHref(href: string): string {
  const m = /^([a-z0-9-]+)\.md(#.*)?$/.exec(href)
  if (m) return guidePath(m[1] ?? '') + (m[2] ?? '')
  return href
}

/** Opens a guide address in place: pushed, so Back returns, and read by App's `popstate`. */
export function openGuide(href: string) {
  window.history.pushState(null, '', href)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

function onGuideClick(e: MouseEvent<HTMLAnchorElement>, href: string) {
  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
  if (guideSlugFrom(href.split('#')[0] ?? '') === null) return
  e.preventDefault()
  openGuide(href)
}

/** Bold, code spans and links, inside one block's text. */
export function inline(text: string, key = 'i'): ReactNode[] {
  const out: ReactNode[] = []
  const re = /`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^)\s]+)\)/g
  let last = 0
  let n = 0
  for (const m of text.matchAll(re)) {
    const at = m.index ?? 0
    if (at > last) out.push(plain(text.slice(last, at), `${key}-t${n}`))
    const k = `${key}-${n++}`
    if (m[1] !== undefined)
      out.push(
        <code key={k}>
          <FilledText text={m[1]} />
        </code>,
      )
    else if (m[2] !== undefined) out.push(<strong key={k}>{inline(m[2], k)}</strong>)
    else {
      const href = guideHref(m[4] ?? '')
      const external = /^https?:/.test(href)
      out.push(
        <a
          key={k}
          className="ctl-link"
          href={href}
          onClick={(e) => onGuideClick(e, href)}
          {...(external ? { target: '_blank', rel: 'noreferrer' } : {})}
        >
          {inline(m[3] ?? '', k)}
        </a>,
      )
    }
    last = at + m[0].length
  }
  if (last < text.length) out.push(plain(text.slice(last), `${key}-t${n}`))
  return out
}

/** Plain text as written, or filled when it carries a token. */
function plain(text: string, key: string): ReactNode {
  return text.includes('{{') ? <FilledText key={key} text={text} /> : text
}

function BlockView({ block, k, tight }: { block: Block; k: string; tight?: boolean }) {
  switch (block.kind) {
    case 'heading': {
      const id = headingId(block.text)
      if (block.level <= 1) return <h1 id={id}>{inline(block.text, k)}</h1>
      if (block.level === 2) return <h2 id={id}>{inline(block.text, k)}</h2>
      return <h3 id={id}>{inline(block.text, k)}</h3>
    }
    case 'para':
      return tight ? <>{inline(block.text, k)}</> : <p>{inline(block.text, k)}</p>
    case 'code':
      return <CodeBlock text={block.text} />
    case 'table':
      return (
        <div className="guide-table" tabIndex={0} role="region" aria-label={block.head.join(', ')}>
          <table>
            <thead>
              <tr>
                {block.head.map((c, j) => (
                  <th key={j} scope="col">
                    {inline(c, `${k}-h${j}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((r, ri) => (
                <tr key={ri}>
                  {r.map((c, j) => (
                    <td key={j}>{inline(c, `${k}-${ri}-${j}`)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    case 'list': {
      const items = block.items.map((item, j) => (
        <li key={j}>
          {item.map((b, bi) => (
            // The item's first paragraph is its text; anything under it keeps its own block.
            <BlockView key={bi} block={b} k={`${k}-${j}-${bi}`} tight={bi === 0 && b.kind === 'para'} />
          ))}
        </li>
      ))
      return block.ordered ? <ol>{items}</ol> : <ul>{items}</ul>
    }
  }
}

export function GuideArticle({ markdown }: { markdown: string }) {
  const blocks = useMemo(() => parseMarkdown(markdown), [markdown])
  return (
    <article className="guide-article">
      {blocks.map((b, j) => (
        <BlockView key={j} block={b} k={`b${j}`} />
      ))}
    </article>
  )
}

/** A link to the guide from another console page, opened in place. */
export function GuideLink({ slug = '', children, className = 'ctl-link', label }: { slug?: string; children: ReactNode; className?: string; label?: string }) {
  const href = guidePath(slug)
  return (
    <a className={className} href={href} aria-label={label} onClick={(e) => onGuideClick(e, href)}>
      {children}
    </a>
  )
}

/**
 * One guide page. `served` is what the deployment serves for the tokens; left
 * out, it is read once from the deployment (never on the fixture build).
 */
export function GuideScreen({ slug, served: given }: { slug: string; served?: GuideServed }) {
  const pages = useMemo(() => guidePages(), [])
  const got = useGuideServed(given)
  const values: GuideValues = { ...got, console: window.location.origin }
  const page = slug === '' ? GUIDE_INDEX : slug
  const markdown = GUIDE_SOURCES[page]
  const at = pages.findIndex((p) => p.slug === page)
  const prev = at > 0 ? pages[at - 1] : undefined
  const next = at >= 0 && at < pages.length - 1 ? pages[at + 1] : undefined
  const [query, setQuery] = useState('')
  const q = query.trim().toLowerCase()
  const hits = q === '' ? [] : pages.filter((p) => p.title.toLowerCase().includes(q))
  const top = useRef<HTMLDivElement | null>(null)

  // A new page starts at its top, or at the heading its address names.
  useEffect(() => {
    setQuery('')
    const id = window.location.hash.replace(/^#/, '')
    const el = id === '' ? null : document.getElementById(decodeURIComponent(id))
    if (el !== null && typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'start' })
    else top.current?.closest('.ctl-scroll')?.scrollTo?.(0, 0)
  }, [page])

  return (
    <GuideValuesContext.Provider value={values}>
      <div className="guide" ref={top}>
        <nav className="guide-bar" aria-label="User guide">
          <GuideLink className="guide-home">User guide</GuideLink>
          <label className="guide-search">
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={`Search ${pages.length} page titles`}
              aria-label="Search the guide's page titles"
            />
          </label>
        </nav>
        {q !== '' && (
          <section className="guide-hits" aria-label="Pages found">
            {hits.length === 0 ? (
              <p>No page title mentions “{query.trim()}”.</p>
            ) : (
              <ul>
                {hits.map((p) => (
                  <li key={p.slug}>
                    <GuideLink slug={p.slug}>{p.title}</GuideLink>
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}
        {markdown === undefined ? (
          <article className="guide-article">
            <h1>No guide page is called {slug}</h1>
            <p>
              <GuideLink>Open the guide's first page</GuideLink>
            </p>
          </article>
        ) : (
          <GuideArticle markdown={markdown} />
        )}
        {(prev !== undefined || next !== undefined) && (
          <nav className="guide-pager" aria-label="Previous and next page">
            {prev !== undefined ? (
              <GuideLink slug={prev.slug}>← {prev.title}</GuideLink>
            ) : (
              <span />
            )}
            {next !== undefined && <GuideLink slug={next.slug}>{next.title} →</GuideLink>}
          </nav>
        )}
      </div>
    </GuideValuesContext.Provider>
  )
}
