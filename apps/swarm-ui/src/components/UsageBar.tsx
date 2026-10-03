/**
 * THE USAGE BAR (components.html A, "Progress and usage bars"), replacing
 * seven bar styles: one 6px track (`--track-h`), and five honest forms.
 *
 *   used / ceiling          the fill, in sky
 *   nearly full / full      amber, AND the triangle with words, so it reads
 *                           in greyscale ("11 / 12, nearly full")
 *   over the ceiling        red to the ceiling's tick, red hatch past it
 *   ceiling unknown         the held units, the rest hatched: "9 held ·
 *                           ceiling unknown" -- never a bar against 0
 *   not measured            the whole track hatched and dashed, no figure
 *
 * HATCHING MEANS "NOT A MEASUREMENT" AND NOTHING ELSE (owner's pick,
 * 2026-10-02). Warn is amber plus the triangle, so the hatch keeps one
 * meaning.
 */
import { BadGlyph, WarnGlyph } from './glyphs'

/** At or above this share of the ceiling, a bar is "nearly full". */
export const NEARLY_FULL = 0.9

export type UsageForm = 'ok' | 'nearly-full' | 'full' | 'over' | 'unknown-ceiling' | 'unmeasured'

/** Which of the forms a reading is. `used === null` is not measured. */
export function usageForm(used: number | null, ceiling: number | null): UsageForm {
  if (used === null) return 'unmeasured'
  if (ceiling === null) return 'unknown-ceiling'
  if (used > ceiling) return 'over'
  if (ceiling > 0 && used === ceiling) return 'full'
  if (ceiling === 0) return used > 0 ? 'over' : 'full'
  if (used / ceiling >= NEARLY_FULL) return 'nearly-full'
  return 'ok'
}

export function UsageBar({
  label,
  used,
  ceiling,
  unit = '',
  note,
  why,
}: {
  label: string
  /** null: not measured. */
  used: number | null
  /** null: the ceiling could not be read or is not set -- not 0. */
  ceiling: number | null
  unit?: string
  /** Words after the figure ("3 ready"). */
  note?: string
  /** Why it is not measured, or why the ceiling is unknown. */
  why?: string
}) {
  const form = usageForm(used, ceiling)
  const u = unit === '' ? '' : ` ${unit}`
  let figure: JSX.Element
  if (form === 'unmeasured') {
    figure = <b className="c-abs">{why ?? 'not measured'}</b>
  } else if (form === 'unknown-ceiling') {
    figure = (
      <b>
        {used}
        {u} held · <span className="c-abs">{why ?? 'ceiling unknown'}</span>
      </b>
    )
  } else {
    const words = form === 'nearly-full' ? 'nearly full' : form === 'full' ? 'full' : form === 'over' ? `over the ceiling of ${ceiling}` : null
    figure = (
      <b>
        {used} / {ceiling}
        {u}
        {words !== null && (
          <span className={form === 'over' ? 'is-bad' : 'is-warn'}>
            {form === 'over' ? <BadGlyph /> : <WarnGlyph />}
            {words}
            {note !== undefined && `, ${note}`}
          </span>
        )}
        {words === null && note !== undefined && <>, {note}</>}
      </b>
    )
  }

  let track: JSX.Element
  const cls = `c-bar is-${form}`
  if (form === 'unmeasured') {
    track = <div className={cls} aria-hidden />
  } else if (form === 'unknown-ceiling') {
    // The held units against nothing known: a short sky stub, the rest hatched.
    track = (
      <div className={cls} aria-hidden>
        <i className="c-f" style={{ width: '12%' }} />
        <i className="c-h" style={{ left: '12%', right: 0 }} />
      </div>
    )
  } else if (form === 'over') {
    const at = ceiling! > 0 ? Math.max(5, Math.min(95, (ceiling! / used!) * 100)) : 0
    track = (
      <div className={cls} aria-hidden>
        <i className="c-f" style={{ width: `${at}%` }} />
        <i className="c-ov" style={{ left: `${at}%`, right: 0 }} />
        <i className="c-tk" style={{ left: `${at}%` }} />
      </div>
    )
  } else {
    const pct = ceiling! > 0 ? Math.min(100, (used! / ceiling!) * 100) : 0
    track = (
      <div className={cls} aria-hidden>
        <i className="c-f" style={{ width: `${pct}%` }} />
      </div>
    )
  }

  return (
    <div
      className="c-ub"
      data-form={form}
      role={form === 'unmeasured' || form === 'unknown-ceiling' ? undefined : 'meter'}
      aria-label={label}
      aria-valuenow={form === 'unmeasured' || form === 'unknown-ceiling' ? undefined : used!}
      aria-valuemin={form === 'unmeasured' || form === 'unknown-ceiling' ? undefined : 0}
      aria-valuemax={form === 'unmeasured' || form === 'unknown-ceiling' ? undefined : Math.max(ceiling!, used!)}
    >
      <div className="c-ubh">
        <span>{label}</span>
        {figure}
      </div>
      {track}
    </div>
  )
}

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
 * THE BARE TRACK (`UsageTrack`): a usage bar with no label and no figure, for
 * a row that prints its own figure beside it -- a pool's Use cell, an
 * account's window, a worker's CPU and memory, a ledger row. It was the local
 * `UtilTrack` in primitives.tsx and moved into the canonical set (#503 swap),
 * so every bar on every screen is drawn from here.
 *
 * IT KEEPS ITS OWN LOOK (`.ctl-util-*`), NOT `.c-bar`'s, ON PURPOSE: the grey
 * no-verdict fill and the textured warn/bad/paused fills are the owner's
 * decision of 2026-09-24 (design-system.md §6.4), held by
 * tests/unit/control_plane/test_state_colour_discriminability.py, because a
 * bare track has no words beside it to carry the verdict the way `UsageBar`'s
 * triangle does. components.html A's sky fill and hatch-only rule would undo
 * that decision, which is the owner's to make (raised with the owner on the #503 pull request).
 *
 * The four things it has to keep apart:
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
export function UsageTrack({
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
 * PROGRESS, ON THE SAME 6px TRACK: `done` of `total`, in a state's hue
 * (components.html A, "Progress and usage bars"; workflows.html's Steps
 * done). A usage bar's amber "nearly full" would be wrong here -- a finished
 * workflow is not a warning -- so progress is its own form of the one track.
 *
 * `unread` steps are drawn HATCHED after the fill: counted but not measured,
 * which is the one thing hatching means. A census with nothing to count
 * (`total === 0`) is the whole track hatched, never an empty bar that reads
 * as 0 done. The words are the accessible name; the bar is their picture.
 */
export type ProgressTone = 'live' | 'park' | 'bad' | 'neu'

export function ProgressBar({
  done,
  total,
  unread = 0,
  tone = 'neu',
  label,
}: {
  done: number
  total: number
  /** Steps whose state could not be read: hatched, after the fill. */
  unread?: number
  tone?: ProgressTone
  /** The sentence the bar draws, as its accessible name. */
  label: string
}) {
  if (total <= 0) return <span className="c-bar is-progress is-unmeasured" role="img" aria-label={label} />
  const pct = (n: number) => `${Math.round((100 * Math.max(0, Math.min(n, total))) / total)}%`
  return (
    <span className={`c-bar is-progress is-${tone}`} role="img" aria-label={label} data-done={done} data-total={total}>
      <i className="c-f" style={{ width: pct(done) }} />
      {unread > 0 && <i className="c-h" style={{ left: pct(done), width: pct(unread) }} />}
    </span>
  )
}
