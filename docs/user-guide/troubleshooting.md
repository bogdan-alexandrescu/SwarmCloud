# Troubleshooting

Find the words you see, then do what the right-hand column says.

## Signing in

| You see | Do this |
|---|---|
| "Your session expired" | Press **Reload to sign in**. |
| "The API answered with a page instead of data" | Reload the page. If a save failed, make the change again. |
| "This account cannot use SwarmCloud" | Sign in with your work account. |
| "Your tenant is disabled" | Ask an admin. |
| "… is for platform admins" or "… is for workspace admins" | That page is not for your role. Press **Open Overview**. |
| "We could not confirm which team you belong to" | Pick a workspace you belong to. The console forgets the old choice for you. |
| `sign-in required for …: run sc login` (Claude Code) | In a terminal, run `uv run sc login`, then try again. |
| `401` with "IAP error code 900" (Claude Code) | The deployment does not accept this sign-in yet. Run `/sc:doctor` and send its output to your platform team. |

## Starting work

| You see | Do this |
|---|---|
| "Not submitted: your workspace is not ready yet" (`WORKSPACE_NOT_READY`) | Press **Finish setup**, or type `/sc:setup`. If it waits for an admin, ask them to approve. |
| "Not submitted: your workspace has no Claude account yet" (`NO_CLAUDE_ACCOUNT`) | [Add a Claude account](claude-account.md), or press **Request a loan**. |
| "repository example-org/example-app is not granted to you: choose it under Access" (`REPOSITORY_NOT_GRANTED`) | Grant it on [Config › GitHub access](/config/github), tab **Choose repositories**, then submit again. Nothing was created. |
| "unknown runner_profile '…'" | Pick one of the profiles the message lists. |
| "runner_profile '…' is disabled" | The platform does not run that profile now. Pick another. |
| `NOT_SUBMITTED` (Claude Code) | Fix what the error names and run again. |
| `SUBMISSION_UNKNOWN` (Claude Code) | Look on [Work › Workflows](/workflows) before you run it again, so it does not run twice. |

## Setup checklist failures

| You see | Do this |
|---|---|
| "no GitHub account connected" | Press **Connect GitHub**. |
| "… isn't installed anywhere yet" | Press **Install the App**, then **Re-check**. |
| "You asked … owners to install SwarmCloud" | Wait for an owner of that organisation to approve on GitHub. Press **Re-check** later. |
| "… is not enabled for you in this tenant" | Press **Enable** on [Config › GitHub access](/config/github). |
| "SwarmCloud is installed on … but not for …" | Ask an organisation owner to add the repository to the installation, then **Re-check**. |
| "… uses SAML single sign-on" | Open the single sign-on link shown, sign in, then **Re-check**. |
| "That sign-in link has expired or was already used" | Press **Connect GitHub** again. Links last 10 minutes and work once. |
| "SwarmCloud's access as … has ended at GitHub" | Press **Reconnect**. Your organisations and repositories are kept. |
| "GitHub did not answer this check" | GitHub was busy; the result is unknown, not failed. Press **Re-check**. |
| "Your workspace request was not approved" | Read the reason. Press **Request again** after 24 hours. |

## Waiting

A waiting agent uses no capacity. The reason shows on its row.

| Reason shown | What it means | Do this |
|---|---|---|
| provider quota | The Claude account's quota is spent. | Nothing: it resumes when the quota resets. |
| provider cooldown | Backing off after provider errors. | Nothing: it retries by itself. |
| waiting on a step | An earlier step is not done. | Nothing: this is normal. |
| waiting for CI | The pull request's checks are running. | Nothing. |
| refused by the cluster, retrying | The cluster had no room. | Nothing: retried at most 10 minutes apart. |
| Your workspace is at its own limit | Too much of your work runs at once. | Wait, or ask an admin to raise the limit. |
| The platform is at its overall limit | Everyone's work is using the room. | Wait. |
| budget used up | The spend limit for this work is reached. | Ask an admin, or raise the schedule's budget. |
| credential missing | No key is set for this workspace. | Set it on [Config › Secrets](/config/secrets). |
| waiting for you, waiting for a member | It asked a question. | [Answer it](answer-questions.md). |
| paused by someone | An operator paused the pool. | Ask an admin. |

## Still stuck

* `/sc check` names what is stuck, why, and the next command.
* `/sc trouble` lists problems across your workspace.
* Search the console's [Help](/help).
