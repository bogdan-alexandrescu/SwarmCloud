// The work section's click-only states (issue 439; the harness and the rule
// are in prosestates.tsx): the timeline's key, filters, pickers and opened
// lane; Outcomes' filters and pickers; Home's folds and rows; the Needs-you
// strip and its answer box; the chooser; the submit forms' menus, folds and
// their answers after Submit; a QA run's dismissed routes. Walked on the
// fixture build where it draws the opener; mounted on hand-built props (with
// fetch stubbed, prosestates.work.mounts.tsx) where it never does.

import type { ProseState } from './prosestates'
import { fold } from './prosestates.agent'
import { click } from './prosestates.shell'

const TIMELINE = 'work/timeline'
const OUTCOMES = '/timeline/outcomes'

export const WORK: readonly ProseState[] = [
  {
    key: 'work/timeline-key',
    group: 'work',
    files: ['TimelineLanes.tsx'],
    shows: '.tl-key.is-open',
    walk: { open: TIMELINE, then: () => click('.tl-key-toggle') },
  },
  {
    key: 'work/timeline-filters',
    group: 'work',
    files: ['TimelineLanes.tsx'],
    shows: '.tl-sheet.is-open',
    walk: { open: TIMELINE, then: () => click('.tl-sheet-toggle') },
  },
  {
    key: 'work/timeline-state-picker',
    group: 'work',
    files: ['TimelineLanes.tsx'],
    shows: '.tl-sheet details.tl-pick[open]',
    walk: { open: TIMELINE, then: () => fold('.tl-sheet', 'State') },
  },
  {
    key: 'work/timeline-lane',
    group: 'work',
    files: ['TimelineLanes.tsx', 'TimelineRows.tsx'],
    shows: '.tl-detail',
    walk: { open: TIMELINE, then: () => click('button.tl-name') },
  },
  {
    key: 'work/outcomes-filters',
    group: 'work',
    files: ['Activity.tsx'],
    shows: '.ol-sheet.is-open',
    walk: { open: OUTCOMES, then: () => click('.ol-sheet-toggle') },
  },
  {
    key: 'work/outcomes-picker',
    group: 'work',
    files: ['Activity.tsx'],
    shows: '.ol-sheet details.tl-pick[open]',
    walk: { open: OUTCOMES, then: () => fold('.ol-sheet', '') },
  },
  {
    key: 'work/needs-you',
    group: 'work',
    files: ['work/NeedsYou.tsx', 'Agents.tsx'],
    shows: '.ny.is-open',
    walk: { open: 'work/running', then: () => click('.ny-bar') },
  },
  {
    key: 'work/needs-you-answer',
    group: 'work',
    files: ['work/NeedsYou.tsx', 'AwaitInputCard.tsx'],
    shows: '.ny-answer',
    walk: {
      open: 'work/running',
      then: () => {
        click('.ny-bar')
        click('button[aria-label^="Answer "]')
      },
    },
  },
  {
    key: 'work/chooser',
    group: 'work',
    files: ['SubmitChooser.tsx'],
    shows: '.sb-chooser',
    walk: { open: 'work/running', then: () => click('a.sk-cta') },
  },
  {
    key: 'work/home-why',
    group: 'work',
    files: ['Home.tsx'],
    shows: 'details.hm-why[open]',
    walk: { open: 'home', then: () => fold('body', '?') },
  },
  {
    key: 'work/home-workflow-row',
    group: 'work',
    files: ['HomeWorkLists.tsx'],
    shows: '.wl-wf.is-open',
    walk: { open: 'home', then: () => click('.wl-disclose') },
  },
  {
    key: 'submit/add-setting',
    group: 'work',
    files: ['Submit.tsx'],
    shows: '.sbf-addset-list',
    walk: { open: 'work/new', then: () => click('.sbf-addset-btn') },
  },
  {
    key: 'submit/runners-held',
    group: 'work',
    files: ['RunnerPicker.tsx', 'BrowserToolPicker.tsx'],
    shows: 'details.sbf-runners-more[open]',
    walk: { open: 'work/new', then: () => click('details.sbf-runners-more > summary') },
  },
]
