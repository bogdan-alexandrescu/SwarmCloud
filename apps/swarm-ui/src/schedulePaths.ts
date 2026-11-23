// The schedules page's addresses (Schedules.tsx), apart from the page so App.tsx
// and the pages that link to a schedule reach them without loading it.

/**
 * The list's address, and one schedule's (`/automate/schedules/<id>`,
 * paths.ts). A tab other than History rides as `tab=`
 * (`/automate/schedules/<id>?tab=gate`) and the editor as `edit=1`
 * (`/automate/schedules/<id>/edit`), so a reload or a link lands there (AUT-11).
 */
export const SCHEDULES_ADDRESS = 'automate/schedules'
export function scheduleAddress(id: string, at: { tab?: string; edit?: boolean; type?: string } = {}): string {
  const q = new URLSearchParams({ schedule: id })
  if (at.tab !== undefined && at.tab !== 'history') q.set('tab', at.tab)
  if (at.edit === true) q.set('edit', '1')
  if (at.type !== undefined) q.set('type', at.type)
  return `${SCHEDULES_ADDRESS}?${q.toString()}`
}

/**
 * The create form's address: `/automate/schedules/new`. A schedule id is `sch_`
 * and twelve hex digits (`schedules.new_schedule_id`), so `new` names none.
 * `?type=<name>` opens it with that type chosen (`scheduleAddress(NEW_SCHEDULE,
 * { type })`, a starter card's Use).
 */
export const NEW_SCHEDULE = 'new'

/**
 * THE LIST'S TWO SCOPES (SCHED-ONE-PAGE, owner decision 2026-10-13,
 * docs/schedules.md §6.1). Schedules are one page: the tenant's own, and for
 * an admin every tenant's (`AllTenantsSchedules`, what Admin › Schedules was).
 * The scope is in the address, `?scope=all`, so an admin can link it;
 * `/admin/schedules` lands there (paths.ts).
 */
export const ALL_TENANTS_SCHEDULES = `${SCHEDULES_ADDRESS}?scope=all`
