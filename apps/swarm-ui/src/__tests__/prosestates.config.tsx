// The config section's click-only states (issue 439; the harness and the rule
// are in prosestates.tsx).

import { fireEvent } from '@testing-library/react'

import type { ProseState } from './prosestates'
import { click } from './prosestates.shell'
import { clickDrawn, drawn, foldDrawn, hover, press } from './prosestates.overview'
import { mountGroupRoles, mountPluginDetail, mountPushTest, mountSetupStrip, mountStepPlugins } from './prosestates.config.mounts'

const WS = 'config/workspace'

export const CONFIG: readonly ProseState[] = [
  {
    key: 'config/github-how',
    group: 'config',
    files: ['Access.tsx'],
    shows: 'details.ac-rule-how[open]',
    walk: { open: 'config/github', then: () => click('details.ac-rule-how > summary') },
  },
  {
    key: 'config/tokens-pat',
    group: 'config',
    files: ['GitTokens.tsx'],
    shows: '#ur-reg',
    walk: { open: 'config/repositories?page=tokens', then: () => clickDrawn('.ur-discl-btn') },
  },
  {
    key: 'config/workspace-people-github',
    group: 'config',
    files: ['WorkspacePage.tsx'],
    shows: 'details.wpg-fold[open]',
    walk: { open: WS, persona: 'platform-admin', then: async () => { await press('main', 'People'); await foldDrawn('main', 'Members') } },
  },
  {
    key: 'config/workspace-roles-why',
    group: 'config',
    files: ['WorkspacePage.tsx'],
    shows: 'details.wpg-why[open]',
    mount: mountGroupRoles,
    act: async () => { await press('main', 'Roles'); await foldDrawn('main', 'Why the default') },
  },
  {
    key: 'config/repository-detail',
    group: 'config',
    files: ['RepositoriesDetail.tsx'],
    shows: '.ur-detail',
    walk: { open: 'config/repositories', then: async () => { await press('.ur-cacts', 'Open'); await drawn('.ur-detail') } },
  },
  {
    key: 'config/repository-cache',
    group: 'config',
    files: ['RepoGraphCache.tsx'],
    shows: '.ur-cache-body',
    walk: { open: 'config/repositories', then: () => clickDrawn('.ur-cache-toggle') },
  },
  {
    key: 'config/tokens-team',
    group: 'config',
    files: ['GitTokens.tsx'],
    shows: '#ur-reg-team',
    walk: { open: 'config/repositories?page=tokens', persona: 'platform-admin', then: () => press('main', '▸ Workspace token') },
  },
  {
    key: 'config/github-push-test',
    group: 'config',
    files: ['ReachableRepositories.tsx'],
    shows: '.ac-fails',
    mount: mountPushTest,
    act: () => press('body', 'Push test'),
  },
  {
    key: 'config/github-push-test-refused',
    group: 'config',
    files: ['ReachableRepositories.tsx'],
    shows: '.ac-fails',
    data: ['Another check is running on this grant.'],
    mount: mountPushTest,
    act: async () => { await press('body', 'Push test'); await press('.ac-fails', 'Run push test') },
  },
  {
    key: 'config/setup-strip-steps',
    group: 'config',
    files: ['ConfigSetup.tsx'],
    shows: '[aria-label="Workspace setup steps"]',
    mount: mountSetupStrip,
    act: () => press('.cfg-strip', 'Show steps'),
  },
  {
    key: 'admin/new-workspace',
    group: 'config',
    files: ['NewWorkspace.tsx'],
    shows: '[role="dialog"]',
    walk: { open: 'admin/tenants', persona: 'platform-admin', then: () => press('main', 'New workspace') },
  },
  {
    key: 'overview/setup-guide-tip',
    group: 'config',
    files: ['Onboarding.tsx'],
    shows: '.c-tip:not([hidden])',
    walk: { open: 'overview/now', then: () => hover('main') },
  },
  {
    key: 'config/plugin-remove',
    group: 'config',
    files: ['PluginDetail.tsx'],
    shows: '[role="dialog"]',
    mount: mountPluginDetail,
    act: () => press('body', 'Remove'),
  },
  {
    key: 'config/plugin-remove-refused',
    group: 'config',
    files: ['PluginDetail.tsx'],
    shows: '[role="dialog"] [role="alert"]',
    data: ['example-plugin is used by nightly-check.'],
    mount: mountPluginDetail,
    act: async () => {
      await press('body', 'Remove')
      fireEvent.change(await drawn('[role="dialog"] input'), { target: { value: 'example-plugin' } })
      await press('[role="dialog"]', 'Remove')
    },
  },
  {
    key: 'config/plugin-update',
    group: 'config',
    files: ['PluginDetail.tsx', 'PluginDiff.tsx'],
    shows: '[role="dialog"]',
    mount: mountPluginDetail,
    act: async () => { await press('.pd-tabs', 'Versions'); await press('body', 'Review the update'); await press('body', 'Update to') },
  },
  {
    key: 'config/step-plugin-sheet',
    group: 'config',
    files: ['PluginSheet.tsx'],
    shows: '.psh-sheet',
    mount: mountStepPlugins,
    act: () => clickDrawn('button[aria-label^="Add a plugin"]'),
  },
  {
    key: 'config/step-managed-sheet',
    group: 'config',
    files: ['PluginSheet.tsx'],
    shows: '.psh-sheet',
    mount: mountStepPlugins,
    act: () => clickDrawn('button[aria-label^="Managed plugins on"]'),
  },
  {
    key: 'config/step-managed-settings',
    group: 'config',
    files: ['PluginSheet.tsx'],
    shows: 'ul.psh-choices[aria-label$="settings for this step"]',
    mount: mountStepPlugins,
    act: async () => { await clickDrawn('button[aria-label^="Managed plugins on"]'); await clickDrawn('button[aria-label$="settings for this step"]') },
  },
  {
    key: 'config/secret-delete',
    group: 'config',
    files: ['WorkspaceSecrets.tsx'],
    shows: '[role="dialog"]',
    walk: { open: 'config/secrets', then: () => clickDrawn('button[aria-label^="Delete "]') },
  },
  {
    key: 'admin/setup-capacity',
    group: 'config',
    files: ['consolefinish/ChecklistFinish.tsx'],
    shows: 'details.cfc-more[open]',
    walk: { open: 'admin/setup', persona: 'platform-admin', then: () => foldDrawn('main', 'Capacity and add-ons') },
  },
]
