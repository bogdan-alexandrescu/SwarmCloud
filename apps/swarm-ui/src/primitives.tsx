import { useId, type ReactNode } from 'react'
import type { TopicId } from './help'
import { HelpNote } from './HelpCard'

/**
 * THE REACT HALF OF THE DESIGN SYSTEM'S PRIMITIVES, WRITTEN ONCE.
 *
 * docs/web-ui/design-system.md §6 gives every primitive one CSS contract, and
 * §9.4 recorded that the React side had not followed: `Overview.Tile` and
 * `AgentDetail.Metric`, `Overview.UtilTrack` and `AgentDetail.Util`, and
 * `Overview.Absent` and `AgentDetail.Absent` were three pairs of independent
 * wrappers around the same three primitives, and they had already diverged.
 * Overview's track drew the measured-zero baseline tick and AgentDetail's did
 * not; Capacity and Holders drew their tracks by hand as well, so a proportion
 * was written six times in four files. Each copy was a place where the honesty
 * rules could be kept on one screen and lost on the next.
 *
 * So this file draws them and every screen calls it. What a screen still owns
 * is its POLICY -- which reading is a verdict, which absence gets a phrase and
 * which a mark, what a tile links to. What it no longer owns is how an absent
 * reading, a measured zero, an unreadable ceiling or an over-full bar is drawn.
 *
 * THE RULES THIS FILE CARRIES, so they are said where they are enforced:
 *
 *   - An absence is never drawn as a measurement. A track nobody measured is
 *     hatched with no fill and no axis; a plain empty track would read as 0%.
 *   - A measured zero is drawn AS a measurement: the baseline tick, not a
 *     zero-width fill that is pixel-for-pixel a widget that failed to paint.
 *   - Colour on a bar is a verdict. A fill with nothing to report is grey; the
 *     three verdict tones and the one documented grey are the only classes a
 *     fill can take, and they are named here as literals so
 *     test_state_colour_discriminability.py can trace every one of them.
 *   - The six kinds of nothing have six words and one set of silhouettes, and
 *     no screen invents a seventh phrasing of "we do not know".
 */

// ---------------------------------------------------------------------------
// The mark -- the six kinds of nothing (design-system.md §6.10, §8.6)
// ---------------------------------------------------------------------------

/**
 * SIX KINDS OF NOTHING, SIX WORDS, ONE SET OF SILHOUETTES.
 *
 * These are `measure.ts`'s words and `styles.css`'s marks. The border style
 * carries the kind -- solid for a measurement, dashed for a failure, hatched
 * for an absence, dotted for a read in flight -- so the six stay apart in
 * greyscale and in the screenshot pasted into an incident channel, which is
 * where these screens are actually read.
 */
export type MarkKind = 'zero' | 'absent' | 'unread' | 'partial' | 'admin' | 'pending'

const MARK_WORD: Readonly<Record<MarkKind, string>> = {
  zero: 'real zero',
  absent: 'not measured',
  unread: 'not read',
  partial: 'partial',
  admin: 'admin only',
  pending: 'reading',
}

/**
 * The mark, and the sentence that used to be a paragraph.
 *
 * The mark is the FACT: two words, always rendered, legible with every help
 * card shut. `say` is the same fact as a sentence, published as the mark's
 * accessible name -- NOT a `title=`, which has no visible anchor and no
 * keyboard route and is why an earlier attempt at this turned the suite red.
 * A paragraph can sit next to a figure it does not describe; an attribute on
 * the figure cannot.
 *
 * `<i>`, not `<span>`: `.ctl-mark` sets its face with the `font` shorthand, so
 * the element's default italic never reaches it, and every screen that already
 * imported the mark drew it as an `<i>`.
 */
export function Mark({ kind, say }: { kind: MarkKind; say: string }) {
  return (
    <i className={`ctl-mark is-${kind}`} role="img" aria-label={say}>
      {MARK_WORD[kind]}
    </i>
  )
}

// ---------------------------------------------------------------------------
// The metric tile -- `.ctl-metric` (design-system.md §6.2)
// ---------------------------------------------------------------------------

/**
 * The tile's states, as classes on the primitive.
 *
 *   absent   the platform did not record it
 *   unread   we failed to read it; the platform may well have the number
 *   reading  the read is still in flight -- not an absence, not yet anything
 *   alert    a measured figure that is a problem
 *   good     a measured figure that is fine and worth saying so
 *
 * The first three are all "no number", and the temptation is to let one fall
 * into the next. The class keeps them apart; what goes in the value slot for
 * each is the screen's to decide (a phrase on the run panel, a mark on the
 * landing strip), because those are two different encodings the owner signed
 * off on and folding one into the other would change what a screen shows.
 */
export type MetricTone = 'absent' | 'unread' | 'reading' | 'alert' | 'good'

/**
 * One fact, its unit, and what it does not include.
 *
 * `explain` publishes a help topic's short form at the label through
 * `aria-describedby` and draws nothing (B7.4): the tile already tells an
 * absence from a zero on the surface, so the glyph was the part that could go.
 *
 * `href` makes the whole fact the doorway to the screen that answers it; a
 * tile with nowhere to go is a plain element rather than an anchor that looks
 * like one. `say` is the tile's accessible name, and nothing renders it.
 */
export function Metric({
  label,
  value,
  unit,
  sub,
  foot,
  tone,
  explain,
  href,
  say,
  className,
}: {
  label: string
  value: ReactNode
  unit?: string | undefined
  /** A qualifier, never a definition: `of 5 reads`, not what the states mean. */
  sub?: ReactNode
  /** Provenance, in the mono chrome layer. */
  foot?: string | undefined
  tone?: MetricTone | undefined
  explain?: TopicId | undefined
  href?: string | undefined
  say?: string | undefined
  /** A screen's own layout hook. Never a state: states are `tone`. */
  className?: string | undefined
}) {
  const descId = useId()
  const Box = href === undefined ? 'div' : 'a'
  return (
    <Box
      className={`ctl-metric${className ? ` ${className}` : ''}${tone ? ` is-${tone}` : ''}`}
      href={href}
      aria-label={say === undefined ? undefined : `${label}. ${say}`}
    >
      <span
        className="ctl-metric-label"
        aria-describedby={explain === undefined ? undefined : descId}
      >
        {label}
        {explain !== undefined && <HelpNote topic={explain} id={descId} />}
      </span>
      <span className="ctl-metric-value">
        {value}
        {unit !== undefined && unit !== '' && <span className="ctl-metric-unit">{unit}</span>}
      </span>
      {sub !== undefined && <span className="ctl-metric-sub">{sub}</span>}
      {foot !== undefined && foot !== '' && <span className="ctl-metric-foot">{foot}</span>}
    </Box>
  )
}

// ---------------------------------------------------------------------------
// The proportion -- `.ctl-util-track` (design-system.md §6.4)
// ---------------------------------------------------------------------------

/**
 * What a fill is allowed to say, spelled as the modifier class it becomes.
 *
 * `is-warn`, `is-bad` and `is-paused` are verdicts and keep their hue and
 * texture. `ov-projected` is the one documented grey: a reading that is real
 * but not current (its window reset, or its poll is past the staleness
 * window), drawn in `--text-faint` beside a live bar's `--text-dim` and never
 * in the amber or red that says a ceiling is being approached NOW. A
 * DIFFERENT grey, not a visibly different one: 1.27:1 in the dark theme and
 * 1.11:1 in the light one (design-system.md §6.4), so in the light theme the
 * `~` Overview writes before the figure is what tells a projected reading
 * from a live one, not this fill. It keeps the name it shipped with, because
 * `test_state_colour_discriminability.py` lists it BY NAME in
 * `DOCUMENTED_GREYS` and resolves it to a text grey. Anything else is the
 * monochrome default.
 *
 * CLASS NAMES RATHER THAN WORDS, on purpose. The colour guard traces a fill's
 * className to the string literals that build it, and every literal it finds
 * is tried as a class the fill may carry. A mapping written as
 * `tone === 'bad' ? ' is-bad'` would hand it `bad` as well, a bare word some
 * unrelated rule could one day paint.
 */
export type TrackTone = 'is-warn' | 'is-bad' | 'is-paused' | 'ov-projected'

/**
 * THE UTILISATION TRACK, and the four things it has to keep apart.
 *
 * `pct === null`  nothing measured it, or there is no ceiling to measure it
 *                 against. Hatched, NO fill and no axis -- an unfilled plain
 *                 track reads as "0% used", which is a claim.
 * `pct === 0`     a MEASURED zero. A visible BASELINE TICK at the origin and
 *                 the inset hairline, so "nothing is in use" is legible as a
 *                 reading. Deliberately not a minimum width on the fill, which
 *                 would say "a little is in use" and make 0 and 0.4% identical
 *                 instead of making 0 and unmeasured different.
 * `pct > 100`     OVER the ceiling. The track then stands for what is in use
 *                 and the ceiling sits inside it: the fill runs to the
 *                 ceiling and the excess is hatched in the failure colour
 *                 rather than clipped, because a bar pinned full hides the one
 *                 thing worth seeing.
 * otherwise       an ordinary fill.
 *
 * `meter` gives the track `role="meter"` and its numbers, for a screen where
 * the track IS the figure (a pool card) rather than a picture beside one.
 */
export function UtilTrack({
  pct,
  tone,
  zeroTitle,
  meter,
}: {
  /** Percent of the ceiling in use. null when nothing measured it. 0 is a reading. */
  pct: number | null
  tone?: TrackTone | undefined
  /** What the baseline tick means, for the one case that needs explaining. */
  zeroTitle?: string | undefined
  meter?: { label: string; now: number; max: number } | undefined
}) {
  if (pct === null) {
    return (
      <span
        className="ctl-util-track is-unknown"
        role={meter === undefined ? undefined : 'meter'}
        aria-label={meter?.label}
      />
    )
  }
  if (pct === 0) {
    return (
      <span
        className="ctl-util-track is-zero"
        title={zeroTitle}
        role={meter === undefined ? undefined : 'meter'}
        aria-label={meter?.label}
        aria-valuenow={meter?.now}
        aria-valuemin={meter === undefined ? undefined : 0}
        aria-valuemax={meter?.max}
      >
        <i className="ctl-util-zero" aria-hidden />
      </span>
    )
  }

  const over = pct > 100
  // When over, the track represents what is in use and the ceiling sits
  // inside it, so the fill is the ceiling's share of the whole.
  const fillPct = over ? (100 / pct) * 100 : Math.max(0, pct)
  // THE CLASSES A FILL CAN CARRY, AS LITERALS IN THIS FILE. The colour guard
  // traces this identifier to these strings; a class that arrived as an
  // opaque prop from another file is one it could not see.
  const fillClass =
    tone === 'is-warn'
      ? ' is-warn'
      : tone === 'is-bad'
        ? ' is-bad'
        : tone === 'is-paused'
          ? ' is-paused'
          : tone === 'ov-projected'
            ? ' ov-projected'
            : ''

  return (
    <span
      className="ctl-util-track"
      role={meter === undefined ? undefined : 'meter'}
      aria-label={meter?.label}
      aria-valuenow={meter?.now}
      aria-valuemin={meter === undefined ? undefined : 0}
      aria-valuemax={meter?.max}
    >
      <i className={`ctl-util-fill${fillClass}`} style={{ width: `${fillPct}%` }} />
      {over && <i className="ctl-util-over" style={{ width: `${100 - fillPct}%` }} />}
    </span>
  )
}

/**
 * One `.ctl-util` row: a name, the track, the figure, and what set it.
 *
 * The four children are the primitive's four grid areas (`.drawer .ctl-util`
 * names them), so they are drawn here in that order and nowhere else. The
 * `title`s are the long forms of three columns that ellipsise; each is
 * optional because not every column has one.
 */
export function UtilRow({
  name,
  nameTitle,
  track,
  figure,
  figureTitle,
  by,
  byTitle,
}: {
  name: ReactNode
  nameTitle?: string | undefined
  track: Parameters<typeof UtilTrack>[0]
  figure: ReactNode
  figureTitle?: string | undefined
  by: ReactNode
  byTitle?: string | undefined
}) {
  return (
    <div className="ctl-util">
      <span className="ctl-util-name" title={nameTitle}>
        {name}
      </span>
      <UtilTrack {...track} />
      <span className="ctl-util-figure" title={figureTitle}>
        {figure}
      </span>
      <span className="ctl-util-by" title={byTitle}>
        {by}
      </span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The empty state -- `.ctl-empty` (design-system.md §6.9)
// ---------------------------------------------------------------------------

/**
 * The four ways a panel can have nothing to draw, which are four different
 * facts and which this platform's defining bug was drawing alike.
 *
 * THE MARK IS THE MARKER. `is-failed`, `is-partial` and `is-admin` are colour,
 * and colour is not a distinction a greyscale screenshot keeps; the mark is two
 * words, a border style and a fill, and it survives both. It sits INSIDE the
 * heading, beside the fact it qualifies, so the two cannot be separated by a
 * layout that wraps. The heading is the fact, three or four words of it; `say`
 * is the sentence, on the mark's accessible name.
 *
 * The shape is fixed (§6.9): mark, heading, at most one sentence, a link out.
 * A panel whose heading already carries the fact passes no children and
 * renders no paragraph. `explain` publishes the standing reasoning at the
 * heading and draws nothing.
 *
 * Anything but a real zero is a `status`: a read failing or coming back
 * partial is news a screen reader should hear when it changes.
 */
export type AbsentKind = 'zero' | 'failed' | 'partial' | 'admin'

/**
 * THE "LINK OUT" THAT ENDS AN EMPTY STATE (§6.9), and the slot that was
 * missing. The shape is four things and this component drew three, so every
 * screen that wanted the fourth hand-built a panel instead -- `Screen`'s
 * `.state` box (CH-10, CP-21) and the Help page's unknown-topic panel (AH-17)
 * among them. It closes the one sentence rather than adding a second, and it
 * is `.ctl-link` -- ink plus an underline -- like every other in-page link.
 */
export interface LinkOut {
  href: string
  label: ReactNode
}

const EMPTY_MARK: Readonly<Record<AbsentKind, MarkKind>> = {
  zero: 'zero',
  failed: 'unread',
  partial: 'partial',
  admin: 'admin',
}

export function Absent({
  kind,
  heading,
  say,
  children,
  foot,
  explain,
  className,
  link,
}: {
  kind: AbsentKind
  heading: string
  /** The fact as a sentence, published as the mark's accessible name. */
  say: string
  /** At most one sentence. Omitted when the heading already carries the fact. */
  children?: ReactNode
  foot?: string | undefined
  explain?: TopicId | undefined
  /** A screen's own layout hook -- e.g. an empty state inside a card. */
  className?: string | undefined
  /** The way out: where to go from here. Closes the sentence, or stands alone. */
  link?: LinkOut | undefined
}) {
  const cls = kind === 'zero' ? '' : ` is-${kind}`
  const descId = useId()
  return (
    <div
      className={`ctl-empty${className ? ` ${className}` : ''}${cls}`}
      role={kind === 'zero' ? undefined : 'status'}
    >
      <h3 aria-describedby={explain === undefined ? undefined : descId}>
        <Mark kind={EMPTY_MARK[kind]} say={say} />{' '}
        {heading}
        {explain !== undefined && <HelpNote topic={explain} id={descId} />}
      </h3>
      {(children !== undefined || link !== undefined) && (
        <p>
          {children}
          {children !== undefined && link !== undefined && ' '}
          {link !== undefined && (
            <a className="ctl-link" href={link.href}>
              {link.label}
            </a>
          )}
        </p>
      )}
      {foot !== undefined && <span className="ctl-empty-foot">{foot}</span>}
    </div>
  )
}
