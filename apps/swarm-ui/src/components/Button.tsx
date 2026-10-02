/**
 * THE BUTTON (components.html A): five kinds, three sizes, ONE disabled
 * treatment and a busy form. It replaces the twelve button styles the live
 * console drew (`.retry`, `.stop-btn`, `.sbf-go`, `.wfp-btn` ...), which used
 * five different disabled opacities and two ideas of what "primary" is.
 *
 *   primary        the sky fill; the one action a region asks for
 *   secondary      the default: a 3:1 `--ctl-bd` outline on the surface
 *   ghost          no outline; Cancel, Hide, Clear
 *   danger         red outline; a destructive action that will still ask
 *   danger-filled  red fill; the confirming click of a destructive action
 *
 * A DISABLED BUTTON SAYS WHY, beside it (`disabledReason`), rather than
 * leaving a reader to guess what would enable it. A BUSY button keeps its
 * width and says what it is doing ("Submitting…"); its spinner is the one
 * animation besides the live pulse, and stops under reduced motion.
 */
import { useId, type AnchorHTMLAttributes, type ButtonHTMLAttributes, type ReactNode } from 'react'
import { WarnGlyph } from './glyphs'

export type ButtonKind = 'primary' | 'secondary' | 'ghost' | 'danger' | 'danger-filled'
export type ButtonSize = 'sm' | 'md' | 'lg'

interface Shape {
  kind?: ButtonKind
  size?: ButtonSize
  /** An icon before the label. With `iconOnly`, the label is the accessible name. */
  icon?: ReactNode
  iconOnly?: boolean
  full?: boolean
}

/** The class list every button-shaped control carries. */
export function buttonClass({ kind = 'secondary', size = 'md', iconOnly = false, full = false }: Shape, busy = false, extra?: string): string {
  return [
    'c-btn',
    kind === 'secondary' ? '' : `is-${kind}`,
    size === 'md' ? '' : `is-${size}`,
    iconOnly ? 'is-icon' : '',
    full ? 'is-full' : '',
    busy ? 'is-busy' : '',
    extra ?? '',
  ]
    .filter(Boolean)
    .join(' ')
}

export interface ButtonProps extends Shape, Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children'> {
  children: ReactNode
  /**
   * Busy: the request is in flight. `true` keeps the label; a string replaces
   * it ("Stopping…"). The button is disabled while busy.
   */
  busy?: boolean | string
  /** Why it is disabled, said next to it. Setting it disables the button. */
  disabledReason?: string
}

export function Button({
  kind,
  size,
  icon,
  iconOnly = false,
  full,
  busy = false,
  disabledReason,
  disabled,
  children,
  className,
  type = 'button',
  ...rest
}: ButtonProps) {
  const whyId = useId()
  const isBusy = busy !== false
  const off = disabled === true || isBusy || disabledReason !== undefined
  const label = typeof busy === 'string' ? busy : children
  const button = (
    <button
      {...rest}
      type={type}
      className={buttonClass({ kind, size, iconOnly, full }, isBusy, className)}
      disabled={off}
      aria-busy={isBusy || undefined}
      aria-describedby={disabledReason === undefined ? rest['aria-describedby'] : whyId}
      aria-label={iconOnly && typeof children === 'string' ? children : rest['aria-label']}
      title={iconOnly && typeof children === 'string' ? children : rest.title}
    >
      {isBusy ? <span className="c-spin" aria-hidden /> : icon}
      {iconOnly ? null : label}
    </button>
  )
  if (disabledReason === undefined) return button
  return (
    <span className="c-btn-wrap">
      {button}
      <span className="c-why" id={whyId}>
        <WarnGlyph />
        {disabledReason}
      </span>
    </span>
  )
}

/** A link drawn as a button: a navigation, never an action. */
export function ButtonLink({
  kind,
  size,
  icon,
  full,
  children,
  className,
  ...rest
}: Shape & AnchorHTMLAttributes<HTMLAnchorElement> & { children: ReactNode; href: string }) {
  return (
    <a {...rest} className={buttonClass({ kind, size, full }, false, className)}>
      {icon}
      {children}
    </a>
  )
}
