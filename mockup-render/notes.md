# Direction: cards — vendor cards with in-row expansion (MOCK-UP)

Built at `?lp=cards` (every hook line marked `// MOCK-HOOK`; new files start `// MOCK-HOOK FILE`).

## The idea

Capacity › LLM Providers has two sub-pages, Plans and API Keys, switched by a segmented control
(`/capacity/accounts` lands on Plans). Each sub-page is two vendor-grouped card lists, Anthropic
then OpenAI. A card is one plan or one key: state dot (a word only when it asks something —
Limited until, Credit gone, Draining, Paused), agents against max, the 5-hour and 7-day bars
(plans) or this month's estimated spend against its cap (keys), today's attempts on it, the
Voice it mark, model classes, added by and when, and one line per lend under it (workspace,
agents against the lend's cap, and for a key its spend against the lend's spend cap). Clicking
a card opens it in place: the actions row (Pause/Drain/Resume, Limits, Lend, Rename, Replace
key, Voice it switch, Delete) and the panel the action opens all sit inside the card. Add plan
and Add key are cards too, opened at the top of their list. Nothing overlays the page.

Voice it › Settings gets a "Dedicated OpenAI key" card with the chain line
Plans → Shared keys → This key, each tier with its availability now, then set by, when,
spend, Replace and Delete. A live Voice it session carries a tier mark (Plan, Shared key,
Voice it key) in its header with a one-line caption for a mid-session change. An agent
attempt carries a Plan or API key chip and one "Moved to API key" line that says the move once; its reason is behind (i).

## + What it gets right

+ Everything about a credential stays on its own card: state, bars, lends and, once open,
  every control. The owner never loses the row they acted on (lend, caps, replace, delete).
+ Lends read at rest: one line per borrower with its cap and its use against it, so an
  over-cap lend is red without opening anything (plans, keys, many).
+ The status word only appears where it asks for something; a healthy card is a dot (keys).
+ Credit gone draws the hourly probe's last result, the next probe and Resume on the card
  itself (exhausted).
+ Add key never shows the value back, not even masked: the new card simply appears with a
  green edge (add-key, add-key-done).
+ Delete is a typed confirmation inside the card; destroyed-at-once and not-revoked-at-the-
  provider live behind the (i) (delete).
+ The phone is the same design in one column with no reflow tricks: cards already stack,
  panels stack their fields (keys-390, add-key-390, lend-390).
+ The tier is visible everywhere the owner looks for it: the attempt chip (attempt-tier),
  the Voice it header (voice-tier), the chain line in Settings (voice-settings).

## ! What it costs

! Density: a card is taller than a table row, so 14 keys and 9 plans run long and a
  side-by-side comparison of spend across keys needs scrolling (many).
! An open card pushes every card under it down; with Lend or Limits open the next vendor
  group can be off screen (lend, caps).
! The vendor grouping is fixed: there is a filter (All / Anthropic / OpenAI / Voice it) but no sort by
  spend or by state across vendors (keys, many).
! Delete draws no line about the provider: that the key is not revoked at the vendor sits
  only behind the heading's (i), so an owner who never opens it may not revoke (delete).
! On a phone the open card's action row wraps to two lines (lend-390).

## Review

Every png was read in light and dark (four Sonnet reviewers, then a re-render). Fixed after
review: the add-plan frame now shows the pasted sign-in code; the attempt's "Moved to API key"
line carries a visible (i) for its reason (it had only a hover title); the phone lend frame no
longer scrolls the action row under the top bar. Left as found, not this direction's code:
Voice it › Settings' Machine card shows "Applied" for both zone and machine (existing page);
the app's Reads dock at the foot of the phone frames has its own horizontal scrollbar (the page
itself is no wider than the viewport: scrollWidth = innerWidth in every frame). The gate's
phone-390 preset renders a 392x849 page.
