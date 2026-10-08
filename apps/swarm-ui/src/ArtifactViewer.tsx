import { Fragment, useCallback, useEffect, useRef, useState, type ReactNode } from 'react'

import { Mark } from './AgentDetail'
import { Button, ButtonLink, Segmented } from './components'
import { artifactRawUrl, loadArtifactContent } from './api'
import { DiffView } from './diff/DiffView'
import { errorHeading, num, type ApiError, type Result } from './fetch'
import { HelpCard } from './HelpCard'
import { useEscapeLayer } from './escape'
import { segments } from './logLines'
import { LogText } from './logMarks'
import {
  artifactKind,
  bytesLabel,
  looksLikeDiff,
  type ArtifactContent,
  type ArtifactKindName,
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
  load: loadContent,
  kind,
  absent,
  backLabel,
  exactBytes = 'raw',
}: {
  taskId: string
  artifact: ArtifactRef
  onClose: () => void
  /**
   * The patch bar's back button, where the viewer is not opened from the
   * Artifacts list: `‹ Artifacts` names a list that the Code section and the
   * checkpoint browser do not have.
   */
  backLabel?: string
  /** Where the content comes from, when it is not the artifact-content route.
   *  CheckpointBrowser passes the checkpoint per-file read, which answers in
   *  the same shape, so one viewer renders both. Must be stable (useCallback):
   *  a new function is a new read. */
  load?: () => Promise<Result<ArtifactContent>>
  /**
   * The SERVER's kind for this file, from the listing (#184). When it is
   * given, the viewer is chosen by it -- one name table, on the server -- and
   * when it is not (an older API, the Details pane's list), by the old name
   * rule in `artifactKind`.
   */
  kind?: ArtifactKindName | null
  /**
   * What an `absent` answer means where this viewer is opened, when it is not
   * the default. A staged input read from its upstream run is absent because
   * the upstream object was removed -- by bucket retention, usually -- which is
   * a different sentence from "the manifest names a file that is not there".
   */
  absent?: { heading: string; say: string }
  /**
   * Which download holds this file's stored bytes exactly, for the not-utf-8
   * mark; see `ExactBytes`. Said by the caller, never inferred from `load`: a
   * caller's own loader is not always the checkpoint read -- the Code tab's
   * handed-on diff passes the artifact content route's answer, and its bytes
   * are in the artifact raw route (#207).
   */
  exactBytes?: ExactBytes
}) {
  const [state, setState] = useState<
    | { kind: 'loading' }
    | { kind: 'error'; error: ApiError }
    | { kind: 'ok'; data: ArtifactContent }
  >({ kind: 'loading' })
  /**
   * Which window is shown: null for the first (from byte 0), else the RAW
   * byte offset the server's `next_offset` named. Paging is by the server's
   * offsets only -- redaction makes the text shorter than the bytes, so no
   * offset is ever computed from the text here.
   */
  const [offset, setOffset] = useState<number | null>(null)

  const load = useCallback(async () => {
    setState({ kind: 'loading' })
    const res = await (loadContent
      ? loadContent()
      : offset === null
        ? loadArtifactContent(taskId, artifact.name)
        : loadArtifactContent(taskId, artifact.name, { offset }))
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
  }, [taskId, artifact.name, loadContent, offset])

  useEffect(() => {
    void load()
  }, [load])

  // A PATCH CARRIES ITS OWN BAR -- `‹ Artifacts`, the name, size and attempt
  // -- so the generic head is not drawn above it a second time.
  const patch = state.kind === 'ok' && showsAsDiff(state.data, kind ?? null)

  // OPENING A FILE IS SEEN, AND ESCAPE CLOSES IT (U10a, owner QA 2026-10-04).
  // The viewer opens under the list, below the fold, so a click on a file
  // looked like nothing happened: it is scrolled into view as it opens.
  // Escape closes it (escape.ts: before the split's own Escape, which would
  // close the agent), and focus goes back to what opened it.
  //
  // ITS TOP, IN ITS OWN PANE (U11a, owner QA 2026-10-04). `block: 'nearest'`
  // brought the viewer's still-reading 200px to the foot of the window, at y
  // 763-971 under the log strip, and the content that landed after grew below
  // the fold; and `scrollIntoView` scrolls every ancestor, the page included.
  // Inside the agent's pane only the pane scrolls, to put the viewer's top at
  // the pane's top, as it opens and once more when its first read lands.
  const box = useRef<HTMLDivElement>(null)
  const opener = useRef<Element | null>(null)
  const landed = state.kind !== 'loading'
  useEffect(() => {
    if (box.current !== null) revealTop(box.current)
  }, [landed])
  useEffect(() => {
    opener.current = typeof document === 'undefined' ? null : document.activeElement
    return () => {
      const back = opener.current
      if (back instanceof HTMLElement && back.isConnected && back !== document.body) back.focus()
    }
  }, [])
  useEscapeLayer(true, onClose)

  return (
    <div className="art-viewer" role="region" aria-label={`Artifact ${artifact.name}`} ref={box}>
      {!patch && (
        <div className="art-head">
          <h3 className="mono">{artifact.name}</h3>
          <button type="button" className="art-close" onClick={onClose} aria-label="Close artifact">
            ✕
          </button>
        </div>
      )}
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
      {state.kind === 'error' && state.error.httpStatus === 413 && (
        // A 413 IS THE SERVER DECLINING, not failing: the checkpoint per-file
        // route answers it for a member past one request's read budget, and
        // says the download is the way to it. Drawn as a failed read it grew
        // "The API failed on this request" and a `try again` that can only
        // ever get the same answer.
        <Refused what="past this request's read budget">{state.error.message}</Refused>
      )}
      {state.kind === 'error' && state.error.httpStatus !== 413 && (
        <div className="ctl-empty is-failed" role="status">
          <h3>
            <Mark
              kind="unread"
              say={`${errorHeading(state.error)}. Nothing may be concluded about this artifact's content: it is not empty and it is not missing, the read did not complete.`}
            />{' '}
            {errorHeading(state.error)}
          </h3>
          <p>{state.error.message}</p>
          <Button className="retry" onClick={() => void load()}>
            try again
          </Button>
        </div>
      )}
      {state.kind === 'ok' && (
        <Body
          data={state.data}
          kind={kind ?? null}
          // THE ALLOWLIST'S VERDICT IS READ ONLY WHERE THERE IS ONE: the
          // checkpoint per-file route, which is the only caller passing its
          // own loader. The artifact route serves `content_type` from the name
          // table since #184, and `null` there is a name the table does not
          // know -- not a refusal -- so reading it as one would draw a binary
          // artifact as "refused".
          verdictByName={loadContent !== undefined}
          absent={absent}
          // Paging needs the artifact route's offsets; a caller's own loader
          // reads one window and says so through `truncated`.
          onPage={loadContent === undefined ? setOffset : null}
          // WHERE THE EXACT BYTES ARE, for a window that showed some as
          // U+FFFD: the caller says so (#207).
          exactBytes={exactBytes}
          // THE WHOLE OBJECT, for a patch this read holds only a window of:
          // the artifact raw route, which the server redacts as it does this
          // one. A caller's own loader has no such route.
          rawUrl={loadContent === undefined ? artifactRawUrl(taskId, artifact.name, 'attachment') : null}
          onClose={onClose}
          backLabel={backLabel}
        />
      )}
    </div>
  )
}

function Body({
  data,
  kind,
  verdictByName,
  absent,
  onPage,
  exactBytes,
  rawUrl,
  onClose,
  backLabel,
}: {
  data: ArtifactContent
  kind: ArtifactKindName | null
  verdictByName: boolean
  absent?: { heading: string; say: string } | undefined
  onPage: ((offset: number | null) => void) | null
  exactBytes: ExactBytes
  rawUrl: string | null
  onClose: () => void
  backLabel: string | undefined
}) {
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
        heading={absent?.heading ?? 'object not in the bucket'}
        say={
          absent?.say ??
          "This task's manifest records this artifact and the object is not there. The manifest is the record of what the worker uploaded, so this is a missing object rather than an artifact the agent never wrote."
        }
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

  // REFUSED BY NAME, before a byte was read. The checkpoint per-file route
  // serves `content_type` -- the text allowlist's verdict on the member's
  // name -- and `null` is the allowlist saying no. It reaches this viewer as
  // `status: binary`, and drawn as `binary` it said `not measured` and "not
  // text": an absence, and a claim about bytes nobody read. The run-output
  // route serves no `content_type` at all, so `undefined` is not `null` and
  // an artifact never lands here.
  if (data.status === 'binary' && verdictByName && data.content_type === null) {
    return (
      <Refused what={`type not on the text allowlist · ${num(data.total_bytes)} bytes`}>
        {data.detail}
      </Refused>
    )
  }

  if (data.status === 'binary') {
    // A FACT, NOT AN ABSENCE (AG-29). This was a `.ctl-empty` carrying the
    // `not measured` mark beside `not text · 5,120 bytes` -- the hatched mark
    // for "nobody recorded this" next to a size somebody did record. Nothing
    // is missing: the bytes are in the bucket, they were measured, and they
    // are not text. So it is the size, in `bytesLabel`'s words as every other
    // size in the drawer, and the way to the object; no mark, no panel tone.
    const size = bytesLabel(data.total_bytes)
    return (
      <div className="art-binary">
        <p
          className="art-binary-head"
          aria-label={`Binary, ${size}. This artifact is not text, so there is no window to render. Its location is below; read it with your own credentials.`}
        >
          binary · {size}
        </p>
        {/* NO PREVIEW, AND SAID AS THAT (viewers.html A): nothing here reads
            bytes that are not text or an image. The download is the raw
            route's, which serves them as stored -- not redacted, because no
            rule can scan them, and the response says so. */}
        <p className="art-binary-say">
          No preview. This file is not text and not an image, so nothing here reads it.{' '}
          {rawUrl !== null ? (
            <>
              <ButtonLink size="sm" className="copy" href={rawUrl} download>
                Download
              </ButtonLink>{' '}
              saves it as stored. It is <b>not redacted</b>: no rule can scan these bytes.
            </>
          ) : (
            <>Download the whole archive to get it; it is not redacted.</>
          )}
        </p>
        <span className="art-binary-meta">
          <span className="mono uri">{data.uri}</span>
          <CopyGsutil uri={data.uri} />
        </span>
      </div>
    )
  }

  const content = data.content ?? ''
  const whole = data.offset === 0 && data.next_offset === null && !data.truncated

  if (showsAsDiff(data, kind)) {
    return (
      <PatchArtifact
        data={data}
        content={content}
        whole={whole}
        rawUrl={rawUrl}
        onClose={onClose}
        backLabel={backLabel}
        provenance={<Provenance data={data} onPage={onPage} exactBytes={exactBytes} />}
      />
    )
  }

  return (
    <>
      <Provenance data={data} onPage={onPage} exactBytes={exactBytes} />
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
        <Rendered name={name} content={content} kind={kind ?? data.kind ?? null} whole={whole} />
      )}
    </>
  )
}

/**
 * Which download holds a window's stored bytes exactly. `raw` is the artifact
 * raw route; `checkpoint` is the whole-checkpoint archive, because the
 * checkpoint per-member read has no raw route of its own (#207).
 */
type ExactBytes = 'raw' | 'checkpoint'

/**
 * The line that says how much of the artifact this is, and what was done to it.
 *
 * Both halves are load-bearing. `truncated` stops a window being read as the
 * whole document, and the redaction count is the difference between "this
 * output is clean" and "this output had four credentials in it and you should
 * rotate them" -- a screen that cannot say the second is hiding an incident.
 */
function Provenance({
  data,
  onPage,
  exactBytes,
}: {
  data: ArtifactContent
  /** Move to another window, by the server's raw offset; null when this read cannot page. */
  onPage: ((offset: number | null) => void) | null
  /** Which download holds the stored bytes exactly; see `ExactBytes`. */
  exactBytes: ExactBytes
}) {
  // A WINDOW THAT STARTS PAST BYTE 0 IS NOT THE WHOLE ARTIFACT EITHER, even
  // when it runs to the end and `truncated` is false: the last window of a
  // paged file is a tail. Before paging existed every window started at 0, so
  // `truncated` alone said it; now the start has to be said too.
  const partial = data.truncated || data.offset > 0
  const next = data.next_offset
  return (
    <div className="art-prov">
      <ul className="ctl-facts">
        {/* THE WINDOW. Four sentences -- "This is a window, not the whole
            artifact. Showing N of M bytes. The rest was not read. Download
            below, or read the object from its uri." -- became the fraction
            and a `partial` mark. The fraction is the fact; that the rest was
            not read is what `partial` means; the two ways to get the whole
            object are the two buttons on this same strip. */}
        <li className={`ctl-fact${partial ? ' is-absent' : ''}`}>
          <b>bytes</b>
          {partial ? (
            <>
              {num(data.returned_bytes)} of {num(data.total_bytes)}{' '}
              <Mark
                kind="partial"
                say={`This is a window, not the whole artifact: from byte ${data.offset}${next !== null ? `, with more from byte ${next}` : ', to the end'}. The rest was not read — download what is shown, read the next window, or read the object from its uri.`}
              />
              {data.offset > 0 && <span className="ctl-sub">from byte {num(data.offset)}</span>}
            </>
          ) : (
            <>{num(data.total_bytes)} · complete</>
          )}
          {onPage !== null && next !== null && (
            <Button size="sm" className="copy" onClick={() => onPage(next)}>
              next window
            </Button>
          )}
          {onPage !== null && data.offset > 0 && (
            <Button size="sm" className="copy" onClick={() => onPage(null)}>
              from the start
            </Button>
          )}
        </li>
        {/* BYTES THAT ARE NOT UTF-8 (#188 review). JSON cannot carry them, so
            the server shows each as U+FFFD and COUNTS them; without the count
            a Latin-1 file reads as the agent's own text with odd glyphs in
            it. The download on this strip saves what is shown, U+FFFD and all.
            The mark names where the exact bytes are, and that differs by
            route: on the Artifacts pane and the Code tab's handed-on diff it is
            the raw route, which serves the stored object as stored; on the
            Checkpoint browser there is no
            per-member raw route, so it is the whole-checkpoint download --
            the server's own `detail` says the same (#207). Absent on an older
            API; zero draws nothing. */}
        {typeof data.invalid_utf8_bytes === 'number' && data.invalid_utf8_bytes > 0 && (
          <li className="ctl-fact is-absent">
            <b>not utf-8</b>
            {num(data.invalid_utf8_bytes)}{' '}
            <Mark
              kind="partial"
              say={`${num(data.invalid_utf8_bytes)} bytes of this window are not UTF-8 and are shown as U+FFFD. ${exactBytes === 'raw' ? 'The stored object holds them exactly; the raw download serves them as stored.' : 'The checkpoint download holds them exactly: this file has no download of its own, and the whole checkpoint archive is its stored copy.'}`}
            />
          </li>
        )}
        {/* THE COUNT IS THE DIFFERENCE between "this output is clean" and
            "this output had four credentials in it and you should rotate
            them", so the count stays on the glass. That masking here does not
            remove them from the bucket is a standing fact about the read path
            and is `#help/masking-is-serve-time`.

            A MEASURED FACT, DRAWN AS ONE (AG-5, owner decision 2026-09-25).
            This wore the `not read` mark -- the dashed silhouette whose one
            meaning is "the read failed" -- and the fact was dimmed
            `.is-absent`, the treatment for a figure nothing measured. The
            count WAS read: it is what the serve path's `redact()` returned
            over these bytes. The kit's six marks are six kinds of nothing and
            this is not one of them, and the decision was not to add a
            seventh. So: no mark, no dimming, and the attention it asks for is
            its INK -- `--warn` above zero, plain at zero (`.art-masked` in
            styles.css). The sentence the mark carried is the topic behind
            the `?` on the key. */}
        <li className="ctl-fact art-redacted">
          {/* THE `?` IS ON THE KEY (AH-24): after the label it explains, as
              Overview's `reads ?` is, and never after the count -- where it
              trailed `masked 4 …` and read as a footnote on the figure. Why it
              is here at all is the note at the end of this fact. */}
          <b>
            masked
            <HelpCard topic="masking-is-serve-time" />
          </b>
          <span className={`art-masked${data.redacted && data.redaction_count > 0 ? ' is-warn' : ''}`}>
            {data.redacted ? data.redaction_count : `0 of ${data.redaction.rules} families`}
          </span>
          {/* THIS SCREEN'S ONE `?` (B7.4), and it is the only one it had. What
              it holds is a property of the SERVING path rather than of this
              artifact: masking happens on the way out, the object in the bucket
              is unchanged, and no label on a count can say that. `0 of N
              families` is the count; the glyph on the key is what the count
              does not mean.

              IT OPENS `masking-is-serve-time` (AG-19). It opened "Credential
              names, never values" -- the rule for how a tenant's secrets are
              NAMED, not what happens to a value found in an artifact. The new
              topic is built from the masked mark's own `say` string. */}
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
    <Button size="sm" className="copy" onClick={save}>
      download what is shown
    </Button>
  )
}

function CopyGsutil({ uri }: { uri: string | null }) {
  if (uri === null) return null
  return (
    <Button
      size="sm"
      className="copy"
      onClick={() => navigator.clipboard?.writeText(`gsutil cp ${uri} .`)}
    >
      copy gsutil
    </Button>
  )
}

/**
 * THE SERVER DECLINED -- which is neither an absence nor a failure, and is
 * drawn as neither.
 *
 * Not `absent` (`not measured`): the object is there. Not `unread` with a
 * retry: nothing failed, and asking again gets the same answer. It is a rule
 * answering -- the same kind of fact as the admin gate, and drawn in its tone
 * (`.ctl-empty.is-refused`) with no retry. There is no seventh mark for it:
 * the six marks are the vocabulary for "we do not know", and a refusal is a
 * thing the server does know and has said. The word leads the heading
 * instead, and the server's own sentence -- which names the way round it --
 * is the body.
 */
function Refused({ what, children }: { what: string; children?: ReactNode }) {
  return (
    <div className="ctl-empty is-refused" role="status">
      <h3>refused · {what}</h3>
      {children !== undefined && children !== null && <p>{children}</p>}
    </div>
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

/**
 * THE VIEWER FOR A KIND. The server's kind when there is one (#184) -- one
 * name table, on the server -- and the old name rule when there is not.
 *
 *   markdown     rendered as React elements, never as HTML
 *   json         pretty-printed, but ONLY when the window is the whole file: a
 *                window of a JSON document does not parse, and a viewer that
 *                reformatted what it could would present a fragment as a
 *                document. A part is shown as served and says so.
 *   ndjson, log  monospace, lines kept as the writer wrote them
 *   text         preformatted
 *
 * An image or a binary file never reaches here from the Artifacts pane --
 * those are drawn from the raw route, or described -- but a name hint is not
 * a verdict, so anything else that arrives as text is shown as text.
 */
function Rendered({
  name,
  content,
  kind,
  whole,
}: {
  name: string
  content: string
  kind: ArtifactKindName | null
  whole: boolean
}) {
  // A diff never reaches here: `Body` hands it to `PatchArtifact` first.
  if (kind !== null) {
    switch (kind) {
      case 'markdown':
        return <MarkdownViews source={content} />
      case 'json':
        return <JsonText source={content} whole={whole} />
      case 'ndjson':
      case 'log':
        // NUMBERED LINES, the server's mask in its own ink (logMarks.tsx),
        // still one `<pre>` so a copy is the window.
        return <LogText name={name} content={content} />
      case 'text':
      case 'image':
      case 'binary':
        return <pre className="art-text">{content}</pre>
    }
  }
  switch (artifactKind(name)) {
    case 'markdown':
      return <MarkdownViews source={content} />
    case 'transcript':
      return <Transcript source={content} />
    // `diff` is drawn by `PatchArtifact` before `Rendered` is reached
    // (`isDiffArtifact` answers yes for every name this case matches).
    case 'diff':
    case 'text':
      return <pre className="art-text">{content}</pre>
  }
}

// ---------------------------------------------------------------------------
// A patch: drawn by the #310 viewer, counted here
// ---------------------------------------------------------------------------

/**
 * Whether an artifact is drawn as a patch, whatever the server's kind (#104).
 * The name table calls a `.patch` `text` -- true of the bytes, silent on how
 * to read them -- so the name is asked here, and a file the table could only
 * call text or a log is sniffed for `diff --git` or a hunk header. Markdown
 * and JSON are not sniffed: a README quoting a hunk is still a README.
 */
export function isDiffArtifact(name: string, kind: ArtifactKindName | null, content: string): boolean {
  return (
    artifactKind(name) === 'diff' || ((kind === null || kind === 'text' || kind === 'log') && looksLikeDiff(content))
  )
}

/** A served read that `Body` hands to the patch viewer: readable, not empty, and a diff. */
function showsAsDiff(data: ArtifactContent, kind: ArtifactKindName | null): boolean {
  if (data.status !== 'ok' || data.content === null || data.content === '') return false
  return isDiffArtifact(data.artifact.name ?? '', kind ?? data.kind ?? null, data.content)
}

/** What a WINDOW of a patch holds whole, and what it cut. */
export interface PatchWindow {
  /** The files wholly inside the window, as patch text: `''` when there are none. */
  patch: string
  /** The window starts inside a file that began before it (a later page's head). */
  before: boolean
  /**
   * The path of the file the window's end cut, from its header -- or `''`
   * when the cut text names none. Null when nothing follows the window.
   */
  after: string | null
}

/**
 * The files of a WINDOW that are whole. A window of a patch starts and ends
 * wherever the byte cap fell, so its first file can begin mid-hunk and its
 * last can stop mid-hunk -- and the parser, rightly, will not read a hunk that
 * is shorter than its header says. Where the window lies in the object is
 * read off the server's offsets, never guessed from the text:
 *
 *   - a window from byte 0 starts at the top of the patch; a later one starts
 *     inside whatever file the page before it cut, up to its first header;
 *   - a window with nothing after it ends at the end of the patch; any other
 *     ends inside its last file, which is named so it can be paged to.
 *
 * Nothing is invented and nothing is repaired. A window with no whole file in
 * it -- one file bigger than the window, or the middle of one -- has an empty
 * `patch`, and the viewer says so rather than reporting a parse error.
 */
export function patchWindow(
  served: string,
  at: { offset: number; next_offset: number | null; truncated: boolean },
): PatchWindow {
  const heads: number[] = []
  const re = /^diff --git /gm
  for (let m = re.exec(served); m !== null; m = re.exec(served)) heads.push(m.index)
  const start = at.offset === 0 ? 0 : (heads[0] ?? served.length)
  const more = at.next_offset !== null || at.truncated
  let end = served.length
  if (more) {
    const last = heads[heads.length - 1]
    // The last file runs to the window's end, so it is the one cut. With no
    // header after `start`, everything from `start` is that one file.
    end = last !== undefined && last >= start ? last : start
  }
  const tail = served.slice(end)
  return {
    patch: served.slice(start, end),
    before: start > 0,
    after: more && tail !== '' ? pathOf(tail) : null,
  }
}

/** The path a cut file's header names: `diff --git a/x b/<path>`, else `+++ b/<path>`, else `''`. */
function pathOf(fragment: string): string {
  const git = /^diff --git a\/.+? b\/(.+)$/m.exec(fragment)
  if (git?.[1]) return git[1]
  const plus = /^\+\+\+ (?:b\/)?(.+?)\t?$/m.exec(fragment)
  return plus?.[1] && plus[1] !== '/dev/null' ? plus[1] : ''
}

/**
 * A PATCH, IN THE #310 VIEWER (owner decision 2026-10-01): a file list on the
 * left, one file on the right, a bar with `‹ Artifacts`, the name, size and
 * attempt, Unified/Split, Copy patch and Download. This file draws no diff of
 * its own. The provenance strip stays under the bar -- truncation and the
 * masked count are shown for a patch as for any artifact.
 *
 * A WINDOW IS NOT THE FILE. When the served window is not the whole patch the
 * bar says `partial`, only the window's whole files are drawn, and the files
 * the window's edges cut are named. Copy says `Copy window` and copies the
 * window. Download fetches the WHOLE object through the artifact raw route
 * -- a blob of the window named `change.diff` would be a patch that applies
 * and is silently incomplete -- and is refused, with the reason, where there
 * is no such route.
 */
function PatchArtifact({
  data,
  content,
  whole,
  rawUrl,
  onClose,
  backLabel,
  provenance,
}: {
  data: ArtifactContent
  content: string
  whole: boolean
  rawUrl: string | null
  onClose: () => void
  backLabel: string | undefined
  provenance: ReactNode
}) {
  const name = data.artifact.name ?? 'change.diff'
  const bar = {
    name,
    size: bytesLabel(data.total_bytes),
    attempt: data.attempt_id,
    onBack: onClose,
    ...(backLabel === undefined ? {} : { backLabel }),
  }
  if (whole) return <DiffView patch={content} {...bar} meta={provenance} />

  const w = patchWindow(content, data)
  const cut = (
    <div className="diff-cut" role="note" aria-label="What this window cut">
      {w.before ? (
        <p>This window starts inside a file the page before it cut; those lines are not drawn here.</p>
      ) : null}
      {w.after !== null ? (
        <p>
          <span className="mono">{w.after === '' ? 'the last file' : w.after}</span> cut by the window, page on.
        </p>
      ) : null}
    </div>
  )
  return (
    <DiffView
      patch={w.patch}
      {...bar}
      copy={{ label: 'Copy window', text: content }}
      download={
        rawUrl !== null
          ? { href: rawUrl }
          : { refused: 'This read is a window of the patch and there is no route to the whole object here. Read it from its uri.' }
      }
      meta={
        <>
          {provenance}
          {cut}
        </>
      }
      empty={
        <p className="diff-empty" role="status">
          No file lies wholly inside this window, so none is drawn.{' '}
          {rawUrl !== null ? 'Page on with next window, or download the whole patch.' : 'Read the whole patch from its uri.'}
        </p>
      }
      note={
        <span className="diff-meta">
          <Mark
            kind="partial"
            say="This is a window of the patch, not the whole of it. Only the files wholly inside the window are drawn; the files its edges cut are named under the bar."
          />{' '}
          partial
        </span>
      }
    />
  )
}

/** What one line of a unified diff is. `meta` is a file header, never a change. */
type DiffLineKind = 'add' | 'del' | 'hunk' | 'meta' | 'ctx'

interface DiffLine {
  kind: DiffLineKind
  /** The line without its marker column (for `+`, `-` and context lines). */
  text: string
}

/**
 * A unified diff's lines, classified -- for COUNTING (`diffStat`), never for
 * drawing: a patch is drawn by the #310 viewer. Unlike `parseUnifiedDiff`,
 * this reads a window cut mid-hunk, which a count over a partial read needs.
 *
 * THE HEADERS ARE THE TRAP. `+++ b/file` starts with `+` and `--- a/file` with
 * `-`, and a reader that went by the first character alone counted every file
 * header as an added and a removed line. They are headers only BEFORE a file's
 * first hunk -- inside a hunk, a removed line whose text begins `--` is a real
 * `---` -- so the parser tracks whether it is in a file's header.
 */
function diffLines(source: string): DiffLine[] {
  const rows = source.split('\n')
  // A trailing newline is the file ending, not an empty context line.
  if (rows.length > 0 && rows[rows.length - 1] === '') rows.pop()
  let header = true
  return rows.map((line) => {
    if (line.startsWith('diff --git ')) {
      header = true
      return { kind: 'meta', text: line }
    }
    if (line.startsWith('@@')) {
      header = false
      return { kind: 'hunk', text: line }
    }
    if (header) return { kind: 'meta', text: line }
    if (line.startsWith('+')) return { kind: 'add', text: line.slice(1) }
    if (line.startsWith('-')) return { kind: 'del', text: line.slice(1) }
    // `\ No newline at end of file` is git's note, not a line of either side.
    if (line.startsWith('\\')) return { kind: 'meta', text: line }
    return { kind: 'ctx', text: line.startsWith(' ') ? line.slice(1) : line }
  })
}

/**
 * Files, insertions and deletions in a diff, counted from its lines -- the
 * `+N −M in K files` Details prints for handed-on code (#278). A file is a
 * `diff --git` header, or, for a plain unified diff with none, a `+++` header.
 * Counted over the text this read served: on a window that is not the whole
 * file the caller must say `partial`, because the rest was not counted.
 */
export function diffStat(source: string): { files: number; insertions: number; deletions: number } {
  const lines = diffLines(source)
  const gitHeaders = lines.filter((l) => l.kind === 'meta' && l.text.startsWith('diff --git ')).length
  const plusHeaders = lines.filter((l) => l.kind === 'meta' && l.text.startsWith('+++ ')).length
  return {
    files: gitHeaders > 0 ? gitHeaders : plusHeaders,
    insertions: lines.filter((l) => l.kind === 'add').length,
    deletions: lines.filter((l) => l.kind === 'del').length,
  }
}

/** JSON, pretty-printed only when this window is the whole file and parses. */
function JsonText({ source, whole }: { source: string; whole: boolean }) {
  if (!whole) {
    return (
      <>
        <p className="art-fallback">
          <Mark
            kind="partial"
            say="This is a window of a JSON file, not the whole of it. A part of a JSON document does not parse, so it is shown exactly as served rather than reformatted."
          />{' '}
          partial, not pretty-printed
        </p>
        <pre className="art-text">{source}</pre>
      </>
    )
  }
  let pretty: string | null
  try {
    pretty = JSON.stringify(JSON.parse(source), null, 2)
  } catch {
    pretty = null
  }
  if (pretty === null) {
    return (
      <>
        <p className="art-fallback">
          <Mark
            kind="absent"
            say="This file is named as JSON and is not valid JSON, so it is shown exactly as it was written."
          />{' '}
          not valid JSON
        </p>
        <pre className="art-text">{source}</pre>
      </>
    )
  }
  return <JsonViews value={JSON.parse(source) as unknown} pretty={pretty} />
}

/**
 * A WHOLE JSON FILE AS A TREE, OR RAW (viewers.html A): the tree opens two
 * levels and folds the rest, an array says how many items it holds, and a
 * masked value is the server's `********` in its own ink. Raw is the
 * pretty-printed text. Only a whole file that parses gets here.
 */
function JsonViews({ value, pretty }: { value: unknown; pretty: string }) {
  const [mode, setMode] = useState<'tree' | 'raw'>('tree')
  return (
    <>
      <Segmented
        className="art-view-seg"
        label="JSON view"
        value={mode}
        options={[
          { key: 'tree', label: 'Tree' },
          { key: 'raw', label: 'Raw' },
        ]}
        onChange={setMode}
      />
      {mode === 'raw' ? (
        <pre className="art-text">{pretty}</pre>
      ) : (
        <ul className="art-json" role="tree" aria-label="JSON tree">
          <JsonNode name={null} value={value} depth={0} />
        </ul>
      )}
    </>
  )
}

function JsonLeaf({ value }: { value: unknown }) {
  if (typeof value === 'string') {
    return (
      <span className="art-json-str">
        &quot;
        {segments(value, '').map((s, i) =>
          s.kind === 'mask' ? (
            <span key={i} className="ag-mask" title="masked by the server at read time">
              {s.text}
            </span>
          ) : (
            <Fragment key={i}>{s.text}</Fragment>
          ),
        )}
        &quot;
      </span>
    )
  }
  return <span className="art-json-lit">{value === null ? 'null' : String(value)}</span>
}

function JsonNode({ name, value, depth }: { name: string | null; value: unknown; depth: number }) {
  const key = name === null ? null : <span className="art-json-key">&quot;{name}&quot;: </span>
  if (value !== null && typeof value === 'object') {
    const entries: [string, unknown][] = Array.isArray(value)
      ? value.map((v, i) => [String(i), v] as [string, unknown])
      : Object.entries(value as Record<string, unknown>)
    const says = Array.isArray(value)
      ? `[ ${entries.length} item${entries.length === 1 ? '' : 's'} ]`
      : `{ ${entries.length} key${entries.length === 1 ? '' : 's'} }`
    return (
      <li role="treeitem" aria-expanded={undefined}>
        <details open={depth < 2}>
          <summary>
            {key}
            <span className="art-json-count">{says}</span>
          </summary>
          <ul role="group">
            {entries.map(([k, v]) => (
              <JsonNode key={k} name={Array.isArray(value) ? null : k} value={v} depth={depth + 1} />
            ))}
          </ul>
        </details>
      </li>
    )
  }
  return (
    <li role="treeitem">
      {key}
      <JsonLeaf value={value} />
    </li>
  )
}

/** Markdown rendered as React elements, or its source (viewers.html A): never HTML built from agent bytes. */
function MarkdownViews({ source }: { source: string }) {
  const [mode, setMode] = useState<'rendered' | 'source'>('rendered')
  return (
    <>
      <Segmented
        className="art-view-seg"
        label="Markdown view"
        value={mode}
        options={[
          { key: 'rendered', label: 'Rendered' },
          { key: 'source', label: 'Source' },
        ]}
        onChange={setMode}
      />
      {mode === 'rendered' ? <Markdown source={source} /> : <pre className="art-text">{source}</pre>}
    </>
  )
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

/** A GFM table's delimiter row: `---|:---:|---`, with or without edge pipes. */
const TABLE_DELIMITER = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/

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

    // THE SOURCE'S NUMBER IS KEPT (#222, post-deploy QA of 2026-09-26). A
    // numbered list whose items are parted by blank lines or by a line that
    // is not an item -- the loose list an agent's answer usually is -- comes
    // out as several lists, and each `<ol>` began at 1, so `1. 2. 3.` read
    // `1. 1. 1.`. Each list now starts at its first item's own number, as
    // CommonMark does; the numbers after it follow from there.
    const bulletRe = /^\s*[-*+]\s+(.*)$/
    const numberRe = /^\s*(\d{1,9})[.)]\s+(.*)$/
    const bullet = bulletRe.exec(line)
    const numbered = numberRe.exec(line)
    if (bullet !== null || numbered !== null) {
      flushParagraph()
      const ordered = bullet === null
      const itemRe = ordered ? numberRe : bulletRe
      const start = numbered !== null && ordered ? Number(group(numbered, 1)) : 1
      const items: string[] = []
      while (i < lines.length) {
        const item = itemRe.exec(at(i))
        if (item === null) break
        items.push(group(item, ordered ? 2 : 1))
        i += 1
      }
      const body = items.map((text, n) => <li key={n}>{inline(text)}</li>)
      out.push(
        ordered ? (
          <ol className="art-list" key={key++} start={start !== 1 ? start : undefined}>
            {body}
          </ol>
        ) : (
          <ul className="art-list" key={key++}>
            {body}
          </ul>
        ),
      )
      continue
    }

    // A TABLE, AS ITS SOURCE (AG-22). This renderer does not lay out tables,
    // and the paragraph branch below joined a table's rows with spaces -- a
    // five-row comparison became one run-on line of pipes. A run of `|` rows,
    // or a row followed by its `---|---` delimiter, is kept line for line in
    // a `pre`, which is readable where a half-parsed table would be wrong.
    if (/^\s*\|/.test(line) || (line.includes('|') && TABLE_DELIMITER.test(at(i + 1)))) {
      flushParagraph()
      const body: string[] = []
      while (i < lines.length && at(i).trim() !== '' && at(i).includes('|')) {
        body.push(at(i))
        i += 1
      }
      out.push(
        <pre className="art-code" key={key++} data-block="table">
          {body.join('\n')}
        </pre>,
      )
      continue
    }

    // ANY OTHER BLOCK THIS RENDERER DOES NOT RECOGNISE, AS ITS SOURCE TOO: an
    // indented code block (four spaces or a tab, not continuing a paragraph)
    // and a raw HTML block. Joined as a paragraph, the first loses its line
    // breaks and the second reads as prose made of tags. No HTML is ever
    // built from either -- the `pre` holds the text.
    if (
      (paragraph.length === 0 && /^( {4}|\t)/.test(line)) ||
      /^\s*<(?:[A-Za-z][\w-]*|!--)[\s/>]/.test(line)
    ) {
      flushParagraph()
      const body: string[] = []
      while (i < lines.length && at(i).trim() !== '') {
        body.push(at(i))
        i += 1
      }
      out.push(
        <pre className="art-code" key={key++} data-block="source">
          {body.join('\n')}
        </pre>,
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

/**
 * One line of markdown drawn inline: its code spans, bold, italic and links,
 * and nothing block-level. For a line quoted out of a transcript (the agent's
 * Details strip), where a raw backtick reads as part of the text.
 */
export function MarkdownInline({ text }: { text: string }) {
  return <>{inline(text)}</>
}

/**
 * `code`, **bold**, *italic* and http(s) links, as elements. Bold and italic
 * are read INSIDE as well (owner QA R9, 2026-10-04): `**Edit `x.py`**` drew
 * its backticks raw, because the bold took the code span as plain text.
 */
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
      out.push(<strong key={key++}>{inline(token.slice(2, -2))}</strong>)
    } else if (token.startsWith('*')) {
      out.push(<em key={key++}>{inline(token.slice(1, -1))}</em>)
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
        <Button size="sm" className="copy" onClick={() => setRaw((v) => !v)}>
          {raw ? 'hide record' : 'show record'}
        </Button>
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

/** True when `el`'s own vertical overflow scrolls (auto or scroll). */
function scrollsY(el: HTMLElement): boolean {
  const y = getComputedStyle(el).overflowY
  return y === 'auto' || y === 'scroll'
}

/**
 * Brings `el`'s top to the top of the agent split's element that scrolls it,
 * moving only that element; outside the split it falls back to
 * `scrollIntoView`, which would move every ancestor, the page included.
 *
 * At 1440 that is the pane. On a phone (G2-01) the pane is `overflow:
 * visible` and the whole `.ag-split` column scrolls, so setting the pane's
 * scrollTop would do nothing and a click on a file would look like nothing
 * happened (U10a/U11a). There the column moves, less the sticky tab row, so
 * the viewer's top is not hidden under the tabs.
 */
export function revealTop(el: HTMLElement): void {
  const pane = el.closest<HTMLElement>('.ag-split-pane')
  if (pane === null) {
    el.scrollIntoView?.({ block: 'start', behavior: 'smooth' })
    return
  }
  const split = pane.parentElement?.closest<HTMLElement>('.ag-split') ?? null
  // The column only when it is the one that scrolls and the pane is not;
  // otherwise the pane, as before.
  const scroller = split !== null && !scrollsY(pane) && scrollsY(split) ? split : pane
  let covered = 0
  if (scroller === split) {
    const edge = Array.from(split.children).find(
      (c): c is HTMLElement => c instanceof HTMLElement && c.classList.contains('ag-tabs-edge'),
    )
    if (edge !== undefined && getComputedStyle(edge).position === 'sticky') {
      covered = edge.getBoundingClientRect().height
    }
  }
  const offset = el.getBoundingClientRect().top - scroller.getBoundingClientRect().top
  scroller.scrollTop = Math.max(0, scroller.scrollTop + offset - covered - 8)
}
