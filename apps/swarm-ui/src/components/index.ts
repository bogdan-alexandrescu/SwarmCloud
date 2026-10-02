/**
 * THE CANONICAL COMPONENT SET (components.html Variant A, owner's pick
 * 2026-10-02). Every screen draws its buttons, chips, pills, tables, cards,
 * banners, bars, dialogs and empty states from here, and never restyles one
 * locally; the rules are src/styles/components.css, the tokens are on :root
 * in styles.css.
 */
import '../styles/components.css'

export { Button, ButtonLink, buttonClass, type ButtonKind, type ButtonSize } from './Button'
export { Chip, Count, Dash, LiveChip, Tag } from './Chip'
export { StatePill, ParkPill, StateMark, PARK_WORD, stateWord, type StateForm } from './StatePill'
export { Card, CardLink, StatTile, type TileValue } from './Card'
export { Table, type Column, type SortState } from './Table'
export { Tabs, Segmented, Breadcrumb, routedClick, type TabDef, type SegOption, type Crumb } from './Tabs'
export { Tooltip, Menu, Dialog, TypedConfirm, CancelWorkflowConfirm, cancelNeedsTypedConfirm, TOOLTIP_DELAY_MS, type MenuItem } from './Overlay'
export { Banner, Toaster, acknowledge, dismissToast, forgetToasts, TOAST_MS, type BannerTone, type Toast } from './Banner'
export { UsageBar, usageForm, NEARLY_FULL, type UsageForm } from './UsageBar'
export { EmptyState, LoadingState, Skeleton, type EmptyKind } from './EmptyState'
export { CodeBlock, DiffBlock, parseDiff, type DiffLine } from './Code'
export { CIcon, type CIconName } from './icons'
