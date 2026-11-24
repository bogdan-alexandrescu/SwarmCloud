// EVERY SCREEN'S STYLESHEET, IN THE SHELL, IN THE ORDER THE CASCADE WAS WRITTEN FOR
// (issue 195's rendered review).
//
// Lazy screens (App.tsx) split the JavaScript; they must not split the
// cascade. When these sheets reached the shell through screens App.tsx
// imported statically, Vite built them into ONE stylesheet, each before
// styles.css (main.tsx imports App before it). A sheet that moved into a lazy
// screen's chunk instead:
//
//  * was missing until that screen had been visited, though other screens
//    use its classes: Help's `help-guide` line (guide.css), `sb-note` on a
//    schedule's page, the admin counts page and the voice Talk page
//    (submit.css), Admin settings' `jb-*` options (keys.css), and more that
//    the lazy-CSS test lists;
//  * applied AFTER styles.css, so where a rule here ties styles.css on
//    specificity it now won: `.adm-changed-none` (admin.css) took the colour
//    from `.ctl-em` (styles.css), and the admin limits and secrets pages
//    changed geometry.
//
// So main.tsx imports this module before App, and every sheet below keeps the
// place it had in the one stylesheet. A screen still imports its own sheet,
// which says what it depends on; the import here only fixes where the rules
// land. A sheet added for a new screen goes here too, in import order, or
// the lazy-CSS test fails if another screen uses its classes.
import './styles/components.css'
import './diff/diff.css'
import './styles/primitives.css'
import './styles/contract-jev.css'
import './work/needs-you.css'
import './styles/capacity.css'
import './styles/repositories.css'
import './styles/forgeperms.css'
import './styles/guide.css'
import './styles/onboarding.css'
import './styles/workspace-settings.css'
import './styles/admin.css'
import './styles/config.css'
import './styles/voice-settings.css'
import './styles/voice-activity.css'
import './styles/talk-stage.css'
import './styles/cost-pill.css'
import './styles/talk-pane.css'
import './styles/voice-telemetry.css'
import './styles/voice-floating.css'
import './styles/roles.css'
import './styles/people.css'
import './styles/schedules.css'
import './styles/accounts-ranked.css'
import './styles/automate-edit.css'
import './styles/workflow-facts.css'
import './styles/timeline.css'
import './styles/automate-sheet.css'
import './styles/automate-cards.css'
import './styles/automate-landing.css'
import './styles/keys.css'
import './styles/submit.css'
import './styles/intake.css'
import './styles/workflow-views.css'
import './styles/step-card.css'
import './styles/repograph.css'
import './styles/changes.css'
import './styles/runs.css'
import './styles/interview.css'
import './styles/plan-brief.css'
import './styles/automate-approvals.css'
import './trace/traceView.css'
import './styles/live.css'
import './styles/theater.css'
import './styles/home-worklists.css'
import './styles/home.css'
import './styles/details.css'
import './styles/agent-tabs.css'
import './styles/agent-questions-row.css'
import './styles/agents.css'
import './styles/plugins.css'
import './styles/admin-plugins.css'
import './styles/notifications.css'
import './styles/help.css'
import './styles/workspace-page.css'
import './styles/console-finish.css'
import './styles/lp-cards.css'
import './styles/accounts-timeline.css'
import './styles/llm-providers.css'
import './styles/overview.css'
import './styles/workflows.css'
import './styles/names.css'
import './work/inspectorShell.css'
import './work/stage-grid.css'
import './work/stage-lineage.css'
import './styles/workflow-questions.css'
