// MOCK-HOOK FILE
/**
 * MOCK-HOOK: invented cards for the ?voicecards=viewer mock-up. A person is
 * planning a small bakery website; the planner has produced one card of every
 * kind the viewer knows, and one it does not. Nothing here is read from the
 * server: the card stream does not carry these kinds yet.
 */

export type ContentType =
  | 'proposal'
  | 'mockup'
  | 'diagram'
  | 'wireframe'
  | 'code'
  | 'diff'
  | 'document'
  | 'test_results'

export interface MockCard {
  card_id: string
  kind: string
  /** The viewer is chosen by this; a type the console does not know gets the generic viewer. */
  content_type: string
  title: string
  state: 'proposed' | 'waiting' | 'done' | 'failed'
  at: string
  body: Record<string, unknown>
}

// ---- the mock-up pages (HTML, drawn only inside a sandboxed iframe) ----------

const SITE_CSS = `
*{box-sizing:border-box;margin:0;padding:0}
body{font:16px/1.55 Georgia,'Times New Roman',serif;color:#3b2a1e;background:#fbf6ee}
nav{display:flex;align-items:center;justify-content:space-between;padding:22px 64px;border-bottom:1px solid #e8dccb}
nav b{font-size:24px;letter-spacing:.5px}
nav span{display:flex;gap:28px;font:15px system-ui,sans-serif}
nav span i{font-style:normal;color:#8a6a4f}
nav span i.on{color:#3b2a1e;border-bottom:2px solid #c0743c}
.cta{display:inline-block;background:#c0743c;color:#fff;border-radius:999px;padding:14px 28px;font:600 16px system-ui,sans-serif}
.ghost{display:inline-block;border:2px solid #3b2a1e;border-radius:999px;padding:12px 26px;font:600 16px system-ui,sans-serif}
.hero{display:grid;grid-template-columns:1.1fr 1fr;gap:48px;padding:72px 64px;align-items:center}
.hero h1{font-size:60px;line-height:1.05;margin-bottom:20px}
.hero p{font-size:20px;color:#6b5240;margin-bottom:32px;max-width:30ch}
.ph{border-radius:18px;background:linear-gradient(135deg,#e7c79d,#c98c55 60%,#9b5d33);min-height:360px;position:relative}
.ph:after{content:attr(data-l);position:absolute;left:16px;bottom:14px;font:13px system-ui,sans-serif;color:#fff8}
.row{display:grid;grid-template-columns:repeat(3,1fr);gap:24px;padding:0 64px 64px}
.tile{background:#fff;border:1px solid #eadfce;border-radius:16px;padding:18px}
.tile .ph{min-height:170px;margin-bottom:14px;border-radius:12px}
.tile h3{font-size:20px}.tile p{font:15px system-ui,sans-serif;color:#7b6250}
.band{background:#3b2a1e;color:#fbf6ee;padding:48px 64px;display:flex;justify-content:space-between;align-items:center}
.band h2{font-size:32px}.band p{font:16px system-ui,sans-serif;opacity:.8}
h2.sec{font-size:34px;padding:56px 64px 24px}
.story{display:grid;grid-template-columns:1fr 1fr;gap:56px;padding:72px 64px}
.story h1{font-size:52px;line-height:1.1;margin-bottom:24px}
.story p{font-size:19px;color:#5d4636;margin-bottom:18px}
.time{display:grid;grid-template-columns:repeat(4,1fr);gap:20px;padding:0 64px 72px}
.time div{border-top:3px solid #c0743c;padding-top:14px;font:15px system-ui,sans-serif;color:#6b5240}
.time b{display:block;font:700 26px Georgia,serif;color:#3b2a1e}
.give{text-align:center;padding:80px 64px 40px}
.give h1{font-size:54px;margin-bottom:16px}.give p{font-size:20px;color:#6b5240;max-width:34ch;margin:0 auto 36px}
.amts{display:flex;justify-content:center;gap:16px;margin-bottom:28px}
.amt{border:2px solid #d9c4a8;border-radius:16px;padding:22px 30px;background:#fff;font:600 18px system-ui,sans-serif;min-width:150px}
.amt small{display:block;font-weight:400;color:#8a6a4f;font-size:14px;margin-top:4px}
.amt.on{border-color:#c0743c;background:#fff3e8}
.meter{max-width:620px;margin:40px auto 0;font:15px system-ui,sans-serif;color:#6b5240;text-align:left}
.meter i{display:block;height:12px;border-radius:6px;background:#eadfce;margin:10px 0}
.meter i b{display:block;height:12px;width:68%;border-radius:6px;background:#c0743c}
footer{padding:40px 64px;font:14px system-ui,sans-serif;color:#8a6a4f;border-top:1px solid #e8dccb}
@media (max-width:600px){
 nav{padding:16px 20px}nav span{display:none}
 .hero,.story{grid-template-columns:1fr;padding:32px 20px;gap:24px}
 .hero h1{font-size:40px}.hero p{font-size:18px}.ph{min-height:220px}
 .row,.time{grid-template-columns:1fr;padding:0 20px 40px}
 .band{flex-direction:column;gap:18px;align-items:flex-start;padding:36px 20px}
 h2.sec{padding:36px 20px 16px;font-size:28px}.story h1{font-size:38px}
 .give{padding:44px 20px 24px}.give h1{font-size:38px}.amts{flex-direction:column}
 .meter{padding:0 20px}footer{padding:28px 20px}
}`

function page(active: string, main: string): string {
  const tab = (t: string) => `<i class="${t === active ? 'on' : ''}">${t}</i>`
  return `<!doctype html><html><head><meta name="viewport" content="width=device-width"><style>${SITE_CSS}</style></head><body>
<nav><b>Crumb &amp; Hearth</b><span>${tab('Bread')}${tab('Our story')}${tab('Pay it forward')}${tab('Order ahead')}</span></nav>
${main}
<footer>Crumb &amp; Hearth · 14 Mill Lane · Open Tue–Sun, 7am until we sell out</footer></body></html>`
}

const HOME = page(
  'Bread',
  `<section class="hero"><div><h1>Slow bread, baked before the town wakes.</h1>
<p>Sourdough, rye and morning buns from one small oven on Mill Lane.</p>
<span class="cta">Order for pickup</span> &nbsp; <span class="ghost">See today's bake</span></div>
<div class="ph" data-l="hero photo: the oven at 5am"></div></section>
<h2 class="sec">Today's bake</h2>
<div class="row"><div class="tile"><div class="ph" data-l="country loaf"></div><h3>Country sourdough</h3><p>48-hour ferment · $9</p></div>
<div class="tile"><div class="ph" data-l="rye"></div><h3>Seeded rye</h3><p>Dense, dark, keeps a week · $10</p></div>
<div class="tile"><div class="ph" data-l="buns"></div><h3>Cardamom buns</h3><p>Saturdays only · $4</p></div></div>
<div class="band"><div><h2>Order by 8pm, collect tomorrow.</h2><p>Pickup 7–11am at the side door.</p></div><span class="cta">Order ahead</span></div>`,
)

const STORY = page(
  'Our story',
  `<section class="story"><div><h1>One oven, two bakers, a lot of flour.</h1>
<p>We started baking for neighbours from a home kitchen in 2021. The queue outside got longer than the street, so we found a corner shop with a wood-fired oven.</p>
<p>Everything is mixed by hand the day before and baked from 4am. When it's gone, it's gone.</p>
<span class="ghost">Meet the bakers</span></div>
<div class="ph" data-l="founders at the bench" style="min-height:460px"></div></section>
<div class="time"><div><b>2021</b>Home kitchen, 12 loaves a week</div><div><b>2022</b>Market stall on Saturdays</div>
<div><b>2024</b>The Mill Lane shop opens</div><div><b>Today</b>400 loaves, and a waiting list for buns</div></div>`,
)

const DONATE = page(
  'Pay it forward',
  `<section class="give"><h1>Buy a loaf for someone else.</h1>
<p>Every loaf you add goes to the community fridge on Friday morning.</p>
<div class="amts"><div class="amt">1 loaf<small>$9</small></div><div class="amt on">3 loaves<small>$27 · most chosen</small></div><div class="amt">A week of bread<small>$60</small></div></div>
<span class="cta">Pay it forward</span>
<div class="meter">This month: 212 of 300 loaves<i><b></b></i>Delivered every Friday with the fridge volunteers.</div></section>`,
)

export interface MockPage {
  key: string
  name: string
  html: string
  /** The page's height in CSS px at each design width, so the frame can be scaled without reading inside it. */
  height: { desktop: number; phone: number }
}

export const MOCKUP_PAGES: readonly MockPage[] = [
  { key: 'home', name: 'Home', html: HOME, height: { desktop: 1420, phone: 2050 } },
  { key: 'story', name: 'Our story', html: STORY, height: { desktop: 980, phone: 1500 } },
  { key: 'donate', name: 'Pay it forward', html: DONATE, height: { desktop: 860, phone: 1100 } },
]

// ---- the wireframe ------------------------------------------------------------

const WIRE_CSS = `*{box-sizing:border-box;margin:0}body{font:14px/1.4 ui-sans-serif,system-ui,sans-serif;color:#555;background:#fff;padding:28px 40px}
.b{border:2px solid #9a9a9a;border-radius:6px;background:#f2f2f2}.l{height:10px;background:#cfcfcf;border-radius:5px;margin:8px 0}
.h{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px}.h .b{width:150px;height:34px}
.g{display:grid;grid-template-columns:1.4fr 1fr;gap:28px}.days{display:flex;gap:10px;margin:12px 0 22px}.days .b{flex:1;height:58px;display:grid;place-items:center;font-weight:600}
.days .on{background:#dcdcdc;border-color:#555}.it{display:grid;grid-template-columns:72px 1fr 90px;gap:14px;align-items:center;padding:12px 0;border-bottom:1px dashed #bbb}
.it .b{height:56px}.q{display:flex;gap:6px}.q span{width:26px;height:26px;border:2px solid #9a9a9a;border-radius:4px;display:grid;place-items:center}
.btn{height:46px;border-radius:23px;background:#7a7a7a;color:#fff;display:grid;place-items:center;font-weight:700;margin-top:18px}
h4{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:#777;margin-bottom:6px}
@media (max-width:600px){body{padding:16px}.g{grid-template-columns:1fr}}`

export const WIREFRAME_HTML = `<!doctype html><html><head><meta name="viewport" content="width=device-width"><style>${WIRE_CSS}</style></head><body>
<div class="h"><div class="b"></div><div class="l" style="width:280px"></div></div>
<div class="g"><div><h4>1 · Pickup day</h4><div class="days"><div class="b">Tue</div><div class="b on">Wed</div><div class="b">Thu</div><div class="b">Fri</div><div class="b">Sat</div></div>
<h4>2 · Your bread</h4>
<div class="it"><div class="b"></div><div><div class="l" style="width:70%"></div><div class="l" style="width:40%"></div></div><div class="q"><span>−</span><span>1</span><span>+</span></div></div>
<div class="it"><div class="b"></div><div><div class="l" style="width:60%"></div><div class="l" style="width:35%"></div></div><div class="q"><span>−</span><span>2</span><span>+</span></div></div>
<div class="it"><div class="b"></div><div><div class="l" style="width:75%"></div><div class="l" style="width:30%"></div></div><div class="q"><span>−</span><span>0</span><span>+</span></div></div></div>
<div><h4>3 · Summary</h4><div class="b" style="padding:18px;height:auto"><div class="l" style="width:80%"></div><div class="l" style="width:55%"></div><div class="l" style="width:65%"></div>
<div class="l" style="width:40%;margin-top:18px;height:16px"></div><div class="btn">Pay &amp; reserve</div></div>
<div class="b" style="margin-top:16px;height:90px"></div></div></div></body></html>`

// ---- diagrams -------------------------------------------------------------------

export interface DNode {
  id: string
  label: string
  sub?: string
  x: number
  y: number
  w: number
  h: number
  shape?: 'box' | 'pill' | 'diamond' | 'dot' | 'store' | 'actor'
  tone?: 'accent' | 'ok' | 'bad' | 'dim'
}

export interface DEdge {
  from: string
  to: string
  label?: string
  /** Bend points between the two nodes, when a straight line would cross something. */
  via?: readonly [number, number][]
  dashed?: boolean
}

export interface DGroup {
  label: string
  x: number
  y: number
  w: number
  h: number
}

export interface Diagram {
  diagram_type: 'architecture' | 'user_flow' | 'state'
  width: number
  height: number
  nodes: DNode[]
  edges: DEdge[]
  groups?: DGroup[]
}

const ARCH: Diagram = {
  diagram_type: 'architecture',
  width: 1180,
  height: 600,
  groups: [
    { label: 'Static host', x: 250, y: 40, w: 300, h: 300 },
    { label: 'Order service', x: 600, y: 40, w: 300, h: 520 },
    { label: 'Outside services', x: 950, y: 40, w: 210, h: 520 },
  ],
  nodes: [
    { id: 'visitor', label: 'Visitor', sub: 'phone or laptop', x: 30, y: 150, w: 170, h: 70, shape: 'actor' },
    { id: 'cdn', label: 'Edge cache', sub: 'pages, images', x: 290, y: 90, w: 220, h: 70 },
    { id: 'pages', label: 'Site pages', sub: 'Home · Story · Pay it forward', x: 290, y: 220, w: 220, h: 80, tone: 'accent' },
    { id: 'api', label: 'Orders API', sub: 'pickup slots, cart', x: 640, y: 90, w: 220, h: 80, tone: 'accent' },
    { id: 'db', label: 'Orders table', sub: 'one row per order', x: 640, y: 250, w: 220, h: 80, shape: 'store' },
    { id: 'jobs', label: 'Nightly bake list', sub: 'runs 8:05pm', x: 640, y: 410, w: 220, h: 80 },
    { id: 'pay', label: 'Card payments', sub: 'hosted checkout', x: 980, y: 90, w: 150, h: 80, tone: 'dim' },
    { id: 'mail', label: 'Email sender', sub: 'receipts', x: 980, y: 250, w: 150, h: 80, tone: 'dim' },
    { id: 'baker', label: 'Bakers', sub: 'printed list', x: 980, y: 410, w: 150, h: 80, shape: 'actor' },
    { id: 'admin', label: 'Shop admin', sub: 'stock, sold-out', x: 290, y: 410, w: 220, h: 80 },
  ],
  edges: [
    { from: 'visitor', to: 'cdn', label: 'HTTPS' },
    { from: 'cdn', to: 'pages' },
    { from: 'cdn', to: 'api', label: '/api/*' },
    { from: 'api', to: 'db', label: 'read / write' },
    { from: 'api', to: 'pay', label: 'checkout' },
    { from: 'pay', to: 'api', label: 'paid webhook', via: [[1055, 200], [880, 200]], dashed: true },
    { from: 'api', to: 'mail', via: [[900, 150], [900, 290]] },
    { from: 'db', to: 'jobs' },
    { from: 'jobs', to: 'baker', label: 'PDF' },
    { from: 'admin', to: 'db', label: 'sold-out flag', via: [[560, 450], [560, 310]] },
  ],
}

const FLOW: Diagram = {
  diagram_type: 'user_flow',
  width: 560,
  height: 640,
  nodes: [
    { id: 's', label: 'Lands on Home', x: 175, y: 10, w: 150, h: 44, shape: 'pill' },
    { id: 'menu', label: "Sees today's bake", x: 170, y: 84, w: 160, h: 54 },
    { id: 'pick', label: 'Adds loaves', x: 185, y: 168, w: 130, h: 54 },
    { id: 'sold', label: 'Sold out?', x: 190, y: 250, w: 120, h: 90, shape: 'diamond' },
    { id: 'wait', label: 'Joins tomorrow’s list', x: 370, y: 268, w: 175, h: 54, tone: 'dim' },
    { id: 'day', label: 'Chooses pickup day', x: 160, y: 370, w: 180, h: 54 },
    { id: 'pay', label: 'Pays', x: 190, y: 454, w: 120, h: 54 },
    { id: 'ok', label: 'Paid?', x: 190, y: 536, w: 120, h: 90, shape: 'diamond' },
    { id: 'done', label: 'Gets receipt email', x: 370, y: 554, w: 175, h: 54, shape: 'pill', tone: 'ok' },
    { id: 'retry', label: 'Card declined', x: 10, y: 554, w: 140, h: 54, tone: 'bad' },
  ],
  edges: [
    { from: 's', to: 'menu' },
    { from: 'menu', to: 'pick' },
    { from: 'pick', to: 'sold' },
    { from: 'sold', to: 'wait', label: 'yes' },
    { from: 'sold', to: 'day', label: 'no' },
    { from: 'day', to: 'pay' },
    { from: 'pay', to: 'ok' },
    { from: 'ok', to: 'done', label: 'yes' },
    { from: 'ok', to: 'retry', label: 'no' },
    { from: 'retry', to: 'pay', via: [[80, 481]], dashed: true, label: 'try again' },
  ],
}

const STATES: Diagram = {
  diagram_type: 'state',
  width: 500,
  height: 570,
  nodes: [
    { id: 'i', label: '', x: 139, y: 4, w: 22, h: 22, shape: 'dot' },
    { id: 'placed', label: 'Placed', x: 90, y: 50, w: 120, h: 50, shape: 'pill' },
    { id: 'paid', label: 'Paid', x: 90, y: 160, w: 120, h: 50, shape: 'pill', tone: 'accent' },
    { id: 'baking', label: 'Baking', x: 90, y: 270, w: 120, h: 50, shape: 'pill' },
    { id: 'ready', label: 'Ready for pickup', x: 70, y: 380, w: 160, h: 50, shape: 'pill' },
    { id: 'collected', label: 'Collected', x: 90, y: 500, w: 120, h: 50, shape: 'pill', tone: 'ok' },
    { id: 'cancelled', label: 'Cancelled', x: 340, y: 50, w: 140, h: 50, shape: 'pill', tone: 'dim' },
    { id: 'refunded', label: 'Refunded', x: 340, y: 270, w: 140, h: 50, shape: 'pill', tone: 'dim' },
    { id: 'expired', label: 'Not collected', x: 340, y: 380, w: 140, h: 50, shape: 'pill', tone: 'bad' },
  ],
  edges: [
    { from: 'i', to: 'placed' },
    { from: 'placed', to: 'paid', label: 'paid' },
    { from: 'paid', to: 'baking', label: '8:05pm list' },
    { from: 'baking', to: 'ready', label: 'out of oven' },
    { from: 'ready', to: 'collected', label: 'scanned' },
    { from: 'placed', to: 'cancelled', label: '30 min unpaid' },
    { from: 'paid', to: 'refunded', label: 'cancel before 8pm', via: [[410, 185]] },
    { from: 'ready', to: 'expired', label: 'after 11am' },
    { from: 'expired', to: 'refunded', label: 'to the fridge' },
  ],
}

// ---- code and diff ----------------------------------------------------------------

const SNIPPET = `// Pickup windows for a given day: 7-11am in 30-minute slots,
// each holding at most 12 orders, closed once the day's dough is spoken for.
export function pickupSlots(day: Date, booked: Map<string, number>, loavesLeft: number): Slot[] {
  if (loavesLeft <= 0 || day.getDay() === 1) return [] // sold out, or Monday
  const slots: Slot[] = []
  for (let minutes = 7 * 60; minutes < 11 * 60; minutes += 30) {
    const key = slotKey(day, minutes)
    const taken = booked.get(key) ?? 0
    slots.push({ key, label: clock(minutes), open: taken < SLOT_CAPACITY, left: SLOT_CAPACITY - taken })
  }
  return slots
}`

const DIFF_ORDER = `@@ -12,9 +12,14 @@
   <h2>Pickup day</h2>
-  <select name="day">
-    <option>Tomorrow</option>
-  </select>
+  <fieldset class="days" aria-label="Pickup day">
+    {#each days as d}
+      <label class:sold={d.soldOut}>
+        <input type="radio" name="day" value={d.key} disabled={d.soldOut} />
+        {d.short}
+      </label>
+    {/each}
+  </fieldset>
   <h2>Your bread</h2>
@@ -40,6 +45,8 @@
   <button type="submit">Pay &amp; reserve</button>
+  <p class="note" hidden={!cart.soldOutRemoved}>Something sold out and was taken off your order.</p>
 </form>`

const DIFF_CART = `@@ -1,8 +1,10 @@
 import { SLOT_CAPACITY } from './config'
+import { pickupSlots } from './slots'

 export function addLoaf(cart, item) {
-  cart.items.push(item)
-  return cart
+  const left = stockFor(item.sku)
+  if (left <= 0) return { ...cart, soldOutRemoved: true }
+  return { ...cart, items: [...cart.items, item] }
 }
@@ -22,7 +24,7 @@
 export function total(cart) {
-  return cart.items.reduce((s, i) => s + i.price, 0)
+  return cart.items.reduce((s, i) => s + i.price * i.qty, 0)
 }`

// ---- the document ------------------------------------------------------------------

export interface DocBlock {
  h?: string
  p?: string
  li?: readonly string[]
}

const SPEC: DocBlock[] = [
  { h: 'Goal' },
  { p: 'Let a customer reserve and pay for bread the evening before, and collect it at the side door without queueing.' },
  { h: 'In scope' },
  { li: ['Order ahead for Tuesday to Sunday, until 8pm the day before', 'Pickup windows of 30 minutes, 7–11am, at most 12 orders each', 'Card payment on a hosted checkout; receipt by email', 'A printed bake list for the bakers at 8:05pm'] },
  { h: 'Out of scope' },
  { li: ['Delivery', 'Accounts and saved cards', 'Wholesale orders'] },
  { h: 'Rules' },
  { li: ['An item that sells out while it sits in a cart is removed, and the customer is told before paying.', 'An order not collected by 11am goes to the community fridge and is refunded.', 'Cancelling before 8pm refunds in full; after 8pm the bread is already in the list.'] },
  { h: 'Open questions' },
  { li: ['Should Pay it forward loaves count against the day’s stock?', 'Do we text a reminder at 7am, or is the receipt enough?'] },
]

// ---- test results --------------------------------------------------------------------

export interface TestCase {
  name: string
  suite: string
  status: 'passed' | 'failed' | 'skipped'
  ms: number
  message?: string
  expected?: string
  received?: string
  at?: string
}

const TESTS: TestCase[] = [
  { name: 'no slots on a Monday', suite: 'slots', status: 'passed', ms: 3 },
  { name: 'eight half-hour slots from 7am', suite: 'slots', status: 'passed', ms: 2 },
  { name: 'a full slot is closed', suite: 'slots', status: 'passed', ms: 2 },
  {
    name: 'a slot with 11 orders still shows 1 left',
    suite: 'slots',
    status: 'failed',
    ms: 4,
    message: 'left is off by one when the slot is nearly full',
    expected: '{ open: true, left: 1 }',
    received: '{ open: true, left: 0 }',
    at: 'slots.test.ts:41',
  },
  { name: 'adds a loaf that is in stock', suite: 'cart', status: 'passed', ms: 1 },
  { name: 'refuses a sold-out loaf and flags it', suite: 'cart', status: 'passed', ms: 1 },
  {
    name: 'total counts quantity',
    suite: 'cart',
    status: 'failed',
    ms: 2,
    message: 'two country loaves cost $18, not $9',
    expected: '18',
    received: '9',
    at: 'cart.test.ts:57',
  },
  { name: 'order closes at 8pm the day before', suite: 'order form', status: 'passed', ms: 6 },
  { name: 'sold-out day cannot be chosen', suite: 'order form', status: 'passed', ms: 9 },
  { name: 'receipt email lists pickup window', suite: 'order form', status: 'passed', ms: 11 },
  { name: 'pay it forward adds to the fridge count', suite: 'donate', status: 'passed', ms: 4 },
  { name: 'bake list sorts by loaf', suite: 'bake list', status: 'passed', ms: 5 },
  { name: 'bake list prints on one page', suite: 'bake list', status: 'skipped', ms: 0, message: 'needs a printer' },
]

// ---- the cards ----------------------------------------------------------------------

export const MOCK_CARDS: readonly MockCard[] = [
  {
    card_id: 'c-proposal',
    kind: 'proposal',
    content_type: 'proposal',
    title: 'Which look for the bakery site?',
    state: 'waiting',
    at: '2026-10-19T11:42:00Z',
    body: {
      question: 'Three directions for the look of the site. Which one should the pages be built in?',
      options: [
        {
          key: 'a',
          label: 'Warm & handmade',
          swatch: ['#fbf6ee', '#c0743c', '#3b2a1e'],
          gets_right: ['Feels like the shop itself', 'Photos of the bread lead every page', 'Serif type reads as craft'],
          costs: ['Needs good photos before launch', 'Heavier pages on a slow phone'],
          recommended: true,
        },
        {
          key: 'b',
          label: 'Clean & modern',
          swatch: ['#ffffff', '#1f6f5c', '#1d1d1d'],
          gets_right: ['Fastest to build', 'Order form is the hero'],
          costs: ['Could be any bakery', 'Less room for the story'],
          recommended: false,
        },
        {
          key: 'c',
          label: 'Market-stall bold',
          swatch: ['#fff2c7', '#d64528', '#18324a'],
          gets_right: ['Stands out on social posts', 'Big prices, big buttons'],
          costs: ['Loud next to the shop’s quiet front', 'Hand-drawn type costs a licence'],
          recommended: false,
        },
      ],
      recommendation: 'Warm & handmade: the site is mostly read by regulars who already know the shop, and it matches the sign.',
      actions: ['pick', 'mix', 'ask_again'],
    },
  },
  {
    card_id: 'c-mockup',
    kind: 'mockup',
    content_type: 'mockup',
    title: 'Site pages: Home, Our story, Pay it forward',
    state: 'waiting',
    at: '2026-10-19T11:47:00Z',
    body: { frames: MOCKUP_PAGES.map((p) => p.key), actions: ['pick_frame', 'comment', 'open_full_size'] },
  },
  {
    card_id: 'c-arch',
    kind: 'diagram',
    content_type: 'diagram',
    title: 'How the site and orders fit together',
    state: 'proposed',
    at: '2026-10-19T11:50:00Z',
    body: { diagram: ARCH, actions: ['edit_by_voice', 'open_full_size', 'file_issue_from_node'] },
  },
  {
    card_id: 'c-flow',
    kind: 'diagram',
    content_type: 'diagram',
    title: 'Ordering bread for pickup',
    state: 'proposed',
    at: '2026-10-19T11:51:00Z',
    body: { diagram: FLOW, actions: ['edit_by_voice', 'open_full_size', 'file_issue_from_node'] },
  },
  {
    card_id: 'c-state',
    kind: 'diagram',
    content_type: 'diagram',
    title: 'An order’s life',
    state: 'proposed',
    at: '2026-10-19T11:52:00Z',
    body: { diagram: STATES, actions: ['edit_by_voice', 'open_full_size', 'file_issue_from_node'] },
  },
  {
    card_id: 'c-wire',
    kind: 'wireframe',
    content_type: 'wireframe',
    title: 'Order-ahead page',
    state: 'waiting',
    at: '2026-10-19T11:55:00Z',
    body: {
      html: WIREFRAME_HTML,
      height: { desktop: 620, phone: 1180 },
      notes: [
        { n: 1, x: 22, y: 22, text: 'Sold-out days greyed, not hidden' },
        { n: 2, x: 30, y: 62, text: 'Quantity steppers, max 6 a loaf' },
        { n: 3, x: 77, y: 34.5, text: 'Total updates as you add' },
      ],
      actions: ['approve', 'comment', 'ask_again'],
    },
  },
  {
    card_id: 'c-code',
    kind: 'code_snippet',
    content_type: 'code',
    title: 'pickupSlots()',
    state: 'proposed',
    at: '2026-10-19T12:01:00Z',
    body: { language: 'TypeScript', path: 'site/js/slots.ts', first_line: 14, code: SNIPPET, actions: ['copy', 'comment'] },
  },
  {
    card_id: 'c-diff',
    kind: 'code_diff',
    content_type: 'diff',
    title: 'Pickup day picker and sold-out guard',
    state: 'waiting',
    at: '2026-10-19T12:04:00Z',
    body: {
      files: [
        { path: 'site/order.html', patch: DIFF_ORDER },
        { path: 'site/js/cart.js', patch: DIFF_CART },
      ],
      actions: ['approve', 'request_changes', 'comment'],
    },
  },
  {
    card_id: 'c-doc',
    kind: 'document',
    content_type: 'document',
    title: 'Spec: order ahead for pickup',
    state: 'proposed',
    at: '2026-10-19T12:06:00Z',
    body: { blocks: SPEC, words: 186, actions: ['approve', 'comment', 'ask_again'] },
  },
  {
    card_id: 'c-tests',
    kind: 'test_results',
    content_type: 'test_results',
    title: 'Order flow tests',
    state: 'failed',
    at: '2026-10-19T12:09:00Z',
    body: { tests: TESTS, ran_in_ms: 1840, actions: ['ask_to_fix', 'rerun_failed', 'file_issue'] },
  },
  {
    card_id: 'c-unknown',
    kind: 'launch_checklist',
    content_type: 'checklist',
    title: 'Before the site goes live',
    state: 'proposed',
    at: '2026-10-19T12:12:00Z',
    body: {
      due: 'Saturday 2 November',
      owner: 'Robin (bakery)',
      items: [
        { item: 'Photos of the four daily loaves', status: 'to do', who: 'Robin' },
        { item: 'Card payments account approved', status: 'done', who: 'Sam' },
        { item: 'Pickup sign on the side door', status: 'to do', who: 'Robin' },
        { item: 'Test order end to end', status: 'blocked', who: 'Agent' },
      ],
      notes: 'The test order waits on the two failing tests.',
    },
  },
]
