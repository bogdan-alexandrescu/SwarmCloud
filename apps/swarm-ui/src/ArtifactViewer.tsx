import { useCallback, useEffect, useState, type ReactNode } from 'react'

import { Mark } from './AgentDetail'
import { loadArtifactContent } from './api'
import { errorHeading, num, type ApiError } from './fetch'
import { HelpCard } from './HelpCard'
import {
  artifactKind,
  type ArtifactContent,
  type ArtifactRef,
} from './types'

/**
 * THE VIEWER. B29, and the largest unbuilt thing in the product.
 *
 * The brief asked for "a way to visualize the resulting work" and "a way to
 * inspect the outputs". Until now `GET /v1/tasks/{id}/artifacts` listed them
 * and nothing anywhere returned the CONTENT of one, so the answer to "what did
 * the agent actually write" was a `gs://` uri and a gcloud session. The runs
 * really do produce the files -- `wf_5e5ad3b6f7da4299a839` left `synthesis.md`,
 * five step markdown files, `claude-transcript.json` and the stream logs.
 *
 * THREE RULES THIS FILE FOLLOWS, and the third is the one that is easy to lose:
 *
 *  1. NEVER CONSTRUCT A LOCATION. The only thing sent to the server is the
 *     artifact's NAME as the manifest spells it. This component holds `uri` and
 *     `key` for display and never puts either in a request.
 *  2. `status` IS READ BEFORE `content`. `null` is absent, unreadable or
 *     binary; `''` is a real, empty artifact that the agent wrote and left
 *     blank. Four different panels, because they are four different facts.
 *  3. TRUNCATION IS SHOWN, ALWAYS. The server caps the window and says so; a
 *     viewer that rendered the text without the banner would turn a capped read
 *     into "that was the whole output", which is precisely the failure the
 *     server went out of its way to make visible.
 *
 * NO HTML IS EVER CONSTRUCTED FROM ARTIFACT BYTES. The markdown renderer below
 * builds React elements; there is no `dangerouslySetInnerHTML` in this file and
 * there must never be one. An artifact is bytes an agent wrote, which makes it
 * the least trustworthy string in this application.
 */

export function ArtifactViewer({
  taskId,
  artifact,
  onClose,
}: {
  taskId: string
  artifact: ArtifactRef
  onClose: () => void
}) {
  const [state, setState] = useState<
    | { kind: 'loading' }
    | { kind: 'error'; error: ApiError }
    | { kind: 'ok'; data: ArtifactContent }
  >({ kind: 'loading' })

  const load = useCallback(async () => {
    setState({ kind: 'loading' })
    const res = await loadArtifactContent(taskId, artifact.name)
    if (res.status === 'ok' || res.status === 'stale') {
      setState({ kind: 'ok', data: res.data })
    } else if (res.status === 'error') {
      setState({ kind: 'error', error: res.error })
    } else {
      // `empty` cannot occur -- `loadArtifactContent` passes `() => false` --
      // but a component that silently rendered nothing for an unhandled
      // Result variant is how a blank panel gets shipped.
      setState({
        kind: 'error',
        error: {
          kind: 'unreachable',
          httpStatus: null,
          code: null,
          message: 'The read returned a shape this viewer does not handle.',
        },
      })
    }
  }, [taskId, artifact.name])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <div className="art-viewer" role="region" aria-label={`Artifact ${artifact.name}`}>
      <div className="art-head">
        <h3 className="mono">{artifact.name}</h3>
        <button type="button" className="art-close" onClick={onClose} aria-label="Close artifact">
          ✕
        </button>
      </div>
      {/* STILL READING IS NOT NOTHING REPORTED (§8.7.1), and the difference is
          drawn rather than written: `.ctl-pending` is the moving, lighter
          surface at the geometry the text will occupy, and the mark says
          `reading` in words for anyone the motion does not reach. */}
      {state.kind === 'loading' && (
        <p className="art-loading">
          <Mark kind="pending" say={`Reading ${artifact.name}. The read is in flight.`} />
          <span className="ctl-pending art-loading-bar" />
        </p>
      )}
      {state.kind === 'error' && (
        <div className="ctl-empty is-failed" role="status">
          <h3>
            <Mark
              kind="unread"
              say={`${errorHeading(state.error)}. Nothing may be concluded about this artifact's content: it is not empty and it is not missing, the read did not complete.`}
            />{' '}
            {errorHeading(state.error)}
          </h3>
          <p>{state.error.message}</p>
          <button type="button" className="retry" onClick={() => void load()}>
            try again
          </button>
        </div>
      )}
      {state.kind === 'ok' && <Body data={state.data} />}
    </div>
  )
}

function Body({ data }: { data: ArtifactContent }) {
  const name = data.artifact.name ?? ''

  // FOUR STATUSES, FOUR MARKS. They were four paragraphs, and the paragraphs
  // did the telling-apart: "the object is not in the bucket" against "the
  // store could not be read" against "not text" is three different remedies
  // and they were three blocks of grey prose that look identical at a glance.
  // The mark does it in one glyph and two words, greyscale-safe, and the
  // paragraph is its accessible name. `detail` is the server's own sentence
  // about THIS read and stays as the empty state's one sentence.
  if (data.status === 'absent') {
    return (
      <Note
        kind="absent"
        heading="object not in the bucket"
        say="This task's manifest records this artifact and the object is not there. The manifest is the record of what the worker uploaded, so this is a missing object rather than an artifact the agent never wrote."
      >
        {data.detail}
      </Note>
    )
  }

  if (data.status === 'unreadable') {
    return (
      <Note
        kind="unread"
        heading="artifact store could not be read"
        say="Nothing may be concluded about this artifact's content — it is not empty and it is not missing; the read did not complete."
      >
        {data.detail}
      </Note>
    )
  }

  if (data.status === 'binary') {
    return (
      <Note
        kind="absent"
        heading={`not text · ${num(data.total_bytes)} bytes`}
        say="This artifact is not text, so there is no window to render. Its location is below; read it with your own credentials."
      >
        <span className="art-binary-meta">
          <span className="mono uri">{data.uri}</span>
          <CopyGsutil uri={data.uri} />
        </span>
      </Note>
    )
  }

  const content = data.content ?? ''

  return (
    <>
      <Provenance data={data} />
      {content === '' ? (
        // A MEASURED ZERO. `''` is a file the agent created and left blank,
        // and it is the one thing here that is a real reading rather than an
        // absence -- so it takes the `real zero` mark and never the em dash.
        <p className="art-empty">
          <Mark
            kind="zero"
            say="This artifact exists and is empty. The read succeeded and the object is zero bytes — the agent created the file and wrote nothing into it."
          />{' '}
          0 bytes
        </p>
      ) : (
        <Rendered name={name} content={content} />
      )}
    </>
  )
}

/**
 * The line that says how much of the artifact this is, and what was done to it.
 *
 * Both halves are load-bearing. `truncated` stops a window being read as the
 * whole document, and the redaction count is the difference between "this
 * output is clean" and "this output had four credentials in it and you should
 * rotate them" -- a screen that cannot say the second is hiding an incident.
 */
function Provenance({ data }: { data: ArtifactContent }) {
  return (
    <div className="art-prov">
      <ul className="ctl-facts">
        {/* THE WINDOW. Four sentences -- "This is a window, not the whole
            artifact. Showing N of M bytes. The rest was not read. Download
            below, or read the object from its uri." -- became the fraction
            and a `partial` mark. The fraction is the fact; that the rest was
            not read is what `partial` means; the two ways to get the whole
            object are the two buttons on this same strip. */}
        <li className={`ctl-fact${data.truncated ? ' is-absent' : ''}`}>
          <b>bytes</b>
          {data.truncated ? (
            <>
              {num(data.returned_bytes)} of {num(data.total_bytes)}{' '}
              <Mark
                kind="partial"
                say="This is a window, not the whole artifact. The rest was not read — download what is shown, or read the object from its uri."
              />
            </>
          ) : (
            <>{num(data.total_bytes)} · complete</>
          )}
        </li>
        {/* THE COUNT IS THE DIFFERENCE between "this output is clean" and
            "this output had four credentials in it and you should rotate
            them", so the count stays on the glass. That masking here does not
            remove them from the bucket is a standing fact about the read path
            and is `#help/credential-names-not-values`. */}
        <li className={`ctl-fact art-redacted${data.redacted ? ' is-absent' : ''}`}>
          <b>masked</b>
          {data.redacted ? (
            <>
              {data.redaction_count}{' '}
              <Mark
                kind="unread"
                say={`${data.redaction_count} credential-shaped value${data.redaction_count === 1 ? ' was' : 's were'} masked in this artifact when it was served. They are still in the object in the bucket; masking here does not remove them from there, and anything recognisable should be rotated.`}
              />
            </>
          ) : (
            <>0 of {data.redaction.rules} families</>
          )}
          <HelpCard topic="credential-names-not-values" />
        </li>
      </ul>
      <span className="art-prov-actions">
        <Download name={data.artifact.name ?? 'artifact'} content={data.content ?? ''} />
        <CopyGsutil uri={data.uri} />
      </span>
    </div>
  )
}

/**
 * Download of what is ON SCREEN, and the label says so.
 *
 * Built from the text already fetched rather than from a second request,
 * because a "download" that quietly fetched the whole object would hand over
 * bytes this viewer never showed and never redacted a second time. When the
 * window was truncated the button says which part it is saving.
 */
function Download({ name, content }: { name: string; content: string }) {
  const save = () => {
    const url = URL.createObjectURL(new Blob([content], { type: 'text/plain;charset=utf-8' }))
    const a = document.createElement('a')
    a.href = url
    a.download = name.replace(/\//g, '-')
    a.click()
    URL.revokeObjectURL(url)
  }
  // A SENTENCE THAT *IS* THE CONTROL (§8.5.3). The label says what the button
  // saves -- what is on screen, not a second unredacted fetch of the whole
  // object -- and that distinction is the reason the button exists at all.
  return (
    <button type="button" className="copy" onClick={save}>
      download what is shown
    </button>
  )
}

function CopyGsutil({ uri }: { uri: string | null }) {
  if (uri === null) return null
  return (
    <button
      type="button"
      className="copy"
      onClick={() => navigator.clipboard?.writeText(`gsutil cp ${uri} .`)}
    >
      copy gsutil
    </button>
  )
}

/**
 * One of the three non-`ok` statuses, as `.ctl-empty` + a mark.
 *
 * `say` is the paragraph that used to be the body. `children` is what the
 * SERVER said about this particular read, which is the only sentence §8.4
 * leaves in an empty state, and it is omitted rather than padded when there is
 * nothing to say.
 */
function Note({
  kind,
  heading,
  say,
  children,
}: {
  kind: 'absent' | 'unread'
  heading: string
  say: string
  children?: ReactNode
}) {
  return (
    <div className={`ctl-empty is-${kind === 'unread' ? 'failed' : 'partial'}`} role="status">
      <h3>
        <Mark kind={kind} say={say} /> {heading}
      </h3>
      {children !== undefined && children !== null && <p>{children}</p>}
    </div>
  )
}

function Rendered({ name, content }: { name: string; content: string }) {
  switch (artifactKind(name)) {
    case 'markdown':
      return <Markdown source={content} />
    case 'transcript':
      return <Transcript source={content} />
    case 'text':
      return <pre className="art-text">{content}</pre>
  }
}

// ---------------------------------------------------------------------------
// Markdown, rendered as React elements and never as HTML
// ---------------------------------------------------------------------------

/**
 * A deliberately small block-level markdown renderer: headings, fenced code,
 * lists, block quotes, rules and paragraphs, with `code`, **bold**, *italic*
 * and http(s) links inline.
 *
 * WHY NOT A LIBRARY. B5 in the build prompt fixes the dependency budget at
 * zero for exactly this class of thing, and every markdown library that is
 * worth having renders to HTML -- which would mean handing agent-written bytes
 * to `dangerouslySetInnerHTML`. The subset below is what a synthesis document
 * actually uses, and anything outside it degrades to its own source text,
 * which is readable rather than wrong.
 *
 * A LINK IS NOT AUTOMATICALLY A LINK. Only `http:` and `https:` become
 * anchors; anything else (`javascript:`, `data:`, a bare scheme an agent
 * invented) renders as its literal text. A viewer that made every string in an
 * artifact clickable would be taking navigation instructions from output.
 */
export function Markdown({ source }: { source: string }) {
  return <div className="art-md">{markdownBlocks(source)}</div>
}

function markdownBlocks(source: string): ReactNode[] {
  const lines = source.split('\n')
  const out: ReactNode[] = []
  let i = 0
  let key = 0

  // `lines[i]` is `string | undefined` under `noUncheckedIndexedAccess`, and
  // an empty line and a line past the end really are different things -- `at`
  // keeps the body total while `i < lines.length` keeps the difference.
  const at = (n: number): string => lines[n] ?? ''
  const group = (m: RegExpExecArray, n: number): string => m[n] ?? ''

  const paragraph: string[] = []
  const flushParagraph = (): void => {
    if (paragraph.length === 0) return
    out.push(<p key={key++}>{inline(paragraph.join(' '))}</p>)
    paragraph.length = 0
  }

  while (i < lines.length) {
    const line = at(i)

    // A fence runs to the next fence or to the end of the document. An
    // unterminated fence is common in agent output and must not swallow the
    // rest of the renderer.
    const fence = /^\s*```(\w*)\s*$/.exec(line)
    if (fence) {
      flushParagraph()
      const body: string[] = []
      i += 1
      while (i < lines.length && !/^\s*```\s*$/.test(at(i))) {
        body.push(at(i))
        i += 1
      }
      i += 1
      out.push(
        <pre className="art-code" key={key++} data-lang={group(fence, 1) || undefined}>
          {body.join('\n')}
        </pre>,
      )
      continue
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line)
    if (heading) {
      flushParagraph()
      const level = group(heading, 1).length
      // Headings inside an artifact are CONTENT, not page structure, so they
      // start at h4 -- the viewer's own h3 is above them. A document whose h1
      // outranked the screen's heading would break the page's outline for
      // anything reading it by headings.
      const Tag = `h${Math.min(6, level + 3)}` as 'h4' | 'h5' | 'h6'
      out.push(
        <Tag className="art-h" key={key++} data-level={level}>
          {inline(group(heading, 2))}
        </Tag>,
      )
      i += 1
      continue
    }

    if (/^\s*(?:-\s*-\s*-|\*\s*\*\s*\*|_\s*_\s*_)[-*_\s]*$/.test(line)) {
      flushParagraph()
      out.push(<hr key={key++} />)
      i += 1
      continue
    }

    if (/^\s*>\s?/.test(line)) {
      flushParagraph()
      const body: string[] = []
      while (i < lines.length && /^\s*>\s?/.test(at(i))) {
        body.push(at(i).replace(/^\s*>\s?/, ''))
        i += 1
      }
      out.push(
        <blockquote className="art-quote" key={key++}>
          {inline(body.join(' '))}
        </blockquote>,
      )
      continue
    }

    const bulletRe = /^\s*[-*+]\s+(.*)$/
    const numberRe = /^\s*\d+[.)]\s+(.*)$/
    const bullet = bulletRe.exec(line)
    const numbered = numberRe.exec(line)
    if (bullet !== null || numbered !== null) {
      flushParagraph()
      const ordered = bullet === null
      const itemRe = ordered ? numberRe : bulletRe
      const items: string[] = []
      while (i < lines.length) {
        const item = itemRe.exec(at(i))
        if (item === null) break
        items.push(group(item, 1))
        i += 1
      }
      const List = ordered ? 'ol' : 'ul'
      out.push(
        <List className="art-list" key={key++}>
          {items.map((text, n) => (
            <li key={n}>{inline(text)}</li>
          ))}
        </List>,
      )
      continue
    }

    if (line.trim() === '') {
      flushParagraph()
      i += 1
      continue
    }

    paragraph.push(line.trim())
    i += 1
  }

  flushParagraph()
  return out
}

/** `code`, **bold**, *italic* and http(s) links, as elements. */
function inline(text: string): ReactNode[] {
  const pattern =
    /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\n]+\*)|(\[[^\]]+\]\([^)\s]+\))|(https?:\/\/[^\s)<>]+)/g
  const out: ReactNode[] = []
  let last = 0
  let key = 0
  let match: RegExpExecArray | null

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) out.push(text.slice(last, match.index))
    const token = match[0]
    if (token.startsWith('`')) {
      out.push(<code key={key++}>{token.slice(1, -1)}</code>)
    } else if (token.startsWith('**')) {
      out.push(<strong key={key++}>{token.slice(2, -2)}</strong>)
    } else if (token.startsWith('*')) {
      out.push(<em key={key++}>{token.slice(1, -1)}</em>)
    } else if (token.startsWith('[')) {
      const parts = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(token)
      const href = parts?.[2]
      const label = parts?.[1]
      out.push(
        href !== undefined && label !== undefined ? link(href, label, key++) : token,
      )
    } else {
      out.push(link(token, token, key++))
    }
    last = match.index + token.length
  }
  if (last < text.length) out.push(text.slice(last))
  return out
}

/**
 * An anchor, but only for a scheme a browser may safely follow from here.
 *
 * `javascript:` and `data:` are the obvious ones; the rule is an allow-list
 * rather than a deny-list because the set of schemes is not ours to enumerate.
 * Anything else renders as its own text, which still tells the reader what the
 * artifact said.
 */
function link(href: string, text: string, key: number): ReactNode {
  const safe = /^https?:\/\//i.test(href)
  if (!safe) return <span key={key}>{text}</span>
  return (
    <a key={key} href={href} target="_blank" rel="noreferrer noopener nofollow">
      {text}
    </a>
  )
}

// ---------------------------------------------------------------------------
// The transcript
// ---------------------------------------------------------------------------

interface TranscriptTurn {
  index: number
  role: string
  text: string
  /** The row as recorded, for the reader who needs the field this view drops. */
  raw: unknown
}

/**
 * `claude-transcript.json` as a conversation rather than as a wall of JSON.
 *
 * The shape is whatever the CLI emitted: `cliagent` parses stdout as one JSON
 * document and falls back to one object per line, so a transcript is an array
 * of events or a single object, and the fields inside are the CLI's, not this
 * platform's. That means the reader below is DEFENSIVE by construction and
 * falls back to the pretty-printed source the moment it cannot recognise
 * something -- a viewer that dropped an event it did not understand would be
 * hiding part of the agent's reasoning.
 */
export function Transcript({ source }: { source: string }) {
  let parsed: unknown
  try {
    parsed = JSON.parse(source)
  } catch {
    // WHY THE RAW SOURCE IS THE RIGHT ANSWER -- a viewer that dropped what it
    // could not parse would be hiding part of the agent's reasoning -- is the
    // reason this fallback exists and is stated in this file's header, where a
    // maintainer reads it. On the glass it is one mark: `not measured`, which
    // is what "this view could not derive a conversation from it" means.
    return (
      <>
        <p className="art-fallback">
          <Mark
            kind="absent"
            say="This artifact is named like a transcript and is not valid JSON, so it is shown exactly as it was written rather than reorganised into a shape it is not."
          />{' '}
          not valid JSON
        </p>
        <pre className="art-text">{source}</pre>
      </>
    )
  }

  const turns = transcriptTurns(parsed)
  if (turns === null) {
    return (
      <>
        <p className="art-fallback">
          <Mark
            kind="absent"
            say="This transcript is valid JSON in a shape this view does not recognise, so it is shown as recorded rather than reorganised into one it is not."
          />{' '}
          shape not recognised
        </p>
        <pre className="art-text">{JSON.stringify(parsed, null, 2)}</pre>
      </>
    )
  }

  return (
    <div className="art-turns">
      {turns.map((turn) => (
        <TranscriptRow key={turn.index} turn={turn} />
      ))}
    </div>
  )
}

function TranscriptRow({ turn }: { turn: TranscriptTurn }) {
  const [raw, setRaw] = useState(false)
  return (
    <div className={`art-turn art-turn-${turn.role.replace(/[^a-z]/gi, '') || 'other'}`}>
      <div className="art-turn-head">
        <span className="art-turn-role">{turn.role}</span>
        <button type="button" className="copy" onClick={() => setRaw((v) => !v)}>
          {raw ? 'hide record' : 'show record'}
        </button>
      </div>
      {turn.text !== '' ? (
        <div className="art-turn-body">{turn.text}</div>
      ) : (
        // `''` IS NOT A FAILURE. An `init` or a `result` event legitimately
        // carries no prose, which is a real zero; the record behind the button
        // beside it is where the rest of the event is.
        <p className="art-fallback">
          <Mark
            kind="zero"
            say="This event carries no text of its own, which is legitimate for an init or a result event. Open the record to see what it does carry."
          />{' '}
          no text
        </p>
      )}
      {raw && <pre className="art-code">{JSON.stringify(turn.raw, null, 2)}</pre>}
    </div>
  )
}

/** Null means "not a shape this view understands", which is an answer. */
function transcriptTurns(parsed: unknown): TranscriptTurn[] | null {
  const events = Array.isArray(parsed) ? parsed : [parsed]
  if (events.length === 0) return null
  const turns: TranscriptTurn[] = []
  for (let i = 0; i < events.length; i += 1) {
    const event = events[i]
    if (typeof event !== 'object' || event === null) return null
    const row = event as Record<string, unknown>
    const role = pickRole(row)
    if (role === null) return null
    turns.push({ index: i, role, text: pickText(row), raw: event })
  }
  return turns
}

function pickRole(row: Record<string, unknown>): string | null {
  const message = row.message
  if (typeof message === 'object' && message !== null) {
    const nested = (message as Record<string, unknown>).role
    if (typeof nested === 'string') return nested
  }
  for (const field of ['role', 'type']) {
    const value = row[field]
    if (typeof value === 'string' && value !== '') {
      const subtype = row.subtype
      return typeof subtype === 'string' && subtype !== '' ? `${value} · ${subtype}` : value
    }
  }
  return null
}

/**
 * The human-readable part of one event, or `''`.
 *
 * `''` is not a failure: an `init` or a `result` event legitimately carries no
 * prose, and the row says so and offers the record. Guessing a summary from
 * the other fields would be inventing narration the agent did not produce.
 */
function pickText(row: Record<string, unknown>): string {
  const direct = row.text ?? row.result ?? row.content
  if (typeof direct === 'string') return direct

  const message = row.message
  const content =
    typeof message === 'object' && message !== null
      ? (message as Record<string, unknown>).content
      : row.content

  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    const parts: string[] = []
    for (const block of content) {
      if (typeof block === 'string') parts.push(block)
      else if (typeof block === 'object' && block !== null) {
        const text = (block as Record<string, unknown>).text
        if (typeof text === 'string') parts.push(text)
      }
    }
    return parts.join('\n')
  }
  return ''
}
