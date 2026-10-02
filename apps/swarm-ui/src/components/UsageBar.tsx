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
