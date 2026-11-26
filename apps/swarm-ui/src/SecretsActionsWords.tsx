import type { ReactNode } from 'react'

import type { ActionsEnvironmentRule } from './api'
import { Chip } from './components'
import { InfoTip, type Said } from './InfoTip'

// THE WORDS OF AN ENVIRONMENT'S BRANCH RULE, and the lines the GitHub Actions
// secrets pane says about it: each one short line, its sentence behind the (i)
// (issue 81). Split from SecretsActions.tsx, which draws them.

/** An environment's rule in the reader's words, short (a chip). */
export function ruleWords(r: ActionsEnvironmentRule | null): string {
  if (r === null) return 'rule not read'
  if (!r.exists) return 'not on GitHub'
  if (r.rule === 'main_only') return `${r.branch} only`
  if (r.rule === 'protected_branches') return 'protected branches'
  if (r.rule === 'custom_branches') return r.branches.length === 0 ? 'selected branches' : `branches: ${r.branches.map((b) => b.name).join(', ')}`
  return 'any branch'
}

export function RuleChip({ rule }: { readonly rule: ActionsEnvironmentRule | null }) {
  if (rule === null) return <Chip>{ruleWords(rule)}</Chip>
  if (!rule.exists) return <Chip tone="bad">{ruleWords(rule)}</Chip>
  return <Chip tone={rule.main_only ? 'live' : 'park'}>{ruleWords(rule)}</Chip>
}

/** What a name may be, behind the field's (i). */
export const NAME_HELP = 'Letters, digits and _; not starting with a digit or GITHUB_. Never the value.'

/** What the pane writes and reads, behind the lead's (i). */
export const leadHelp = (repository: string) =>
  `Written into ${repository} on GitHub. SwarmCloud reads names and update times, never values, and keeps only the name, who set it and when.`

/** SwarmCloud creates no environment: one short line, its sentence behind the (i). */
export function NoCreateNote() {
  return (
    <p className="sa-note">
      Create environments on GitHub, then Check rule.{' '}
      <InfoTip
        about="creating an environment"
        tip="SwarmCloud does not create environments yet: create one on GitHub, then Check rule reads its branch rule."
      />
    </p>
  )
}

/** Who can read a secret written there, in one line. */
export function readersOf(environment: string | null, rule: ActionsEnvironmentRule | null, repository: string): ReactNode {
  if (environment === null) return <>Read by every workflow in {repository}.</>
  const mainOnly = rule !== null && rule.main_only
  const words = ruleWords(rule)
  let by = 'by its rule'
  let whom = `its rule (${words})`
  if (words === 'any branch') {
    by = 'from any branch'
    whom = words
  }
  if (mainOnly) by = `on ${rule.branch} only`
  return (
    <>
      Read by workflows in <b>{environment}</b>, {by}.{' '}
      <InfoTip
        about="who reads it"
        tip={
          mainOnly
            ? `Read by workflows that run in ${environment}, on ${rule.branch} only. SwarmCloud grants nothing further.`
            : `Read by workflows that run in ${environment}, which ${whom} can deploy to.`
        }
      />
    </>
  )
}

/** Why writing to `rule`'s environment asks for its name: one short line, and its sentence (issue 81). */
export function openRule(rule: ActionsEnvironmentRule | null, environment: string): Required<Said> {
  if (rule === null) {
    return {
      line: 'Rule not read: treated as open.',
      tip: `${environment}'s branch rule was not read, so it is treated as open to any branch.`,
    }
  }
  const words = ruleWords(rule)
  return {
    line: words === 'any branch' ? 'A workflow on any branch can read it.' : `Not ${rule.branch} alone: its rule lets others deploy.`,
    tip: `${environment} lets ${words} deploy: a workflow there can read this secret.`,
  }
}

/** Who else a rule lets deploy, in one short line. */
export function othersLine(rule: ActionsEnvironmentRule): string {
  if (rule.rule === 'any_branch') return 'A workflow on any branch can read it.'
  return `Not ${rule.branch} alone: ${rule.rule === 'protected_branches' ? 'protected' : 'selected'} branches can deploy.`
}
