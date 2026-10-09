# The repository graph's overlays: communities, an issue's territory, lanes in flight (design, lane KG8-MOCK)

Row KG8 of [knowledge-graph.md](knowledge-graph.md) §6 builds the console's
community layer and its issue and lane overlays (§4.7), and its acceptance is
"owner's pick from a mock-up first, as RI13 did". This is that mock-up.

The page is [`docs/web-ui/mockups/graph-overlays.html`](../web-ui/mockups/graph-overlays.html).
It has four variants, each drawn in both of the repository graph's views:
**Structure** (the console's SVG) and **Network** (vis-network). GR3 built both
views in PR #929, which was not on main at `2887032` when this was drawn
(2026-10-09). Each variant has a 1280px desktop frame and a 390px phone frame,
in light and dark. The frames are clickable: the layer toggles, Structure /
Network, opening a region, selecting a lane, and the matrix cells all work.
**Nothing here is built.** KG2 (communities) and KG3 (the `/communities` and
`/territory` routes) are not built either. This note records what each variant
shows, what it costs, which one to build, and the questions only the owner can
answer.

**What the page draws, and how much of it is real.**

* **The graph is this repository's.** It has 339 modules and 1,575 import
  edges under `apps/` at `2887032`, read from the Python and TypeScript
  imports. The UI and the Python services share no import, so the Network
  view shows two components.
* **The communities are a stand-in.** There are 25 of them, from Louvain
  clustering (resolution 3, seed 7) in place of KG2's, which does not exist
  yet. Each one is named after its most-imported module, for example
  `swarm-api · forge` and `swarm-ui · api`.
* **The territory is computed as knowledge-graph.md §4.1 describes it:**
  * 3 **declared** files a plan would name for the example issue
    (`issueruns.py`, `repoindex.py`, `RunIndex.tsx`);
  * 27 **expanded** files, which are their importers and imports at depth 1;
  * 7 **seams**, which are the expanded modules with fan-in of 47 or more
    (`types.ts`, `models.py`, `components/index.ts`, `api.ts`, `fetch.ts`,
    `states.py`, `errors.py`).

  That is 37 modules in 13 communities.
* **Example data, labelled as such:**
  * the issue;
  * the two lanes' states (KG1 and DIFF2, with file lists their design rows
    name);
  * a second, invented PR.

  #929 is real, and so is its file list.
* **The Network frames are SVG, not vis-network.** They are drawn with a
  precomputed force layout so that they look like GR3's canvas. The costs
  below were measured with vis-network itself.

## 1. The short answer

**Build variant 2, Regions, with variant 4's per-community cards as its phone
view.** It is the only variant that shows all three layers at 2,000 modules
without drawing a hairball. It is also the shape both design notes already
chose:

* knowledge-graph.md §4.7 asks to "collapse a community to a node";
* [graph-rendering.md](https://github.com/bogdan-alexandrescu/SwarmCloud/blob/gfy-graph-rendering/docs/design/graph-rendering.md)
  §3.4 says the explorer "must start aggregated and open by focus".

The communities that hold a declared file are that focus. Variant 2 spends no
hue per community, so it stays inside the five series of
[design-system.md](../web-ui/design-system.md) §1.6. At 2,034 modules it draws
in 33 ms in Structure. In Network it builds in 126 ms, provided the
aggregation is handed to vis-network ready-made (§4).

## 2. The marks every variant shares

The variants differ in **arrangement**, not in vocabulary. A mark means the
same thing in all four, so a pick can mix them.

| Layer | Mark | Why this mark |
|---|---|---|
| declared file | solid ink ring | Selection and focus are ink, never the accent (design-system.md §1.3) |
| expanded territory | dashed ink ring | Same family as declared, and weaker |
| seam | diamond | A shape, not a colour: seams are the merge-conflict hot spots, so they must read in greyscale |
| lane or PR on a **declared** file | red ring, `--s-bad` | A collision. CLAUDE.md: "two issues that edit the same file are one lane" |
| lane or PR elsewhere in the territory | dashed amber ring, `--s-warn` | An overlap to look at, not a refusal |
| which lane | letter badge A–D | Four lanes would need four more hues; letters cost none |
| new file in a lane | listed as "+ name (new)", no node | A file the index has not seen has no node, and drawing one would invent it |

Every frame also lists the overlap **in words** beside the graph, because
colour is never the only channel. In Network that list is the only accessible
form, because a canvas has no DOM nodes (graph-rendering.md §3.3).

## 3. The four variants

| | Communities | Issue territory | Lanes and PRs |
|---|---|---|---|
| **1 · Paint** | node fill in the community's hue; Structure's clusters become community boxes outlined in it | rings and diamonds on the flat graph | red/amber rings + letter badges on the nodes |
| **2 · Regions** (recommended) | one region per community: a box in Structure, a circle on the community meta-graph in Network, with edge width = imports between communities | the communities holding a declared file **open** and show their modules with the territory marks; closed ones show "n declared · n expanded · n seam" | letter badges on the region, coloured by the worst overlap inside it |
| **3 · Lens** | grouping only: expanded files are grouped by community in Structure; faint hulls in Network | **Structure becomes a territory map** read left to right (declared and seams → expanded by community → lanes); Network dims the 302 modules outside it | lanes as cards with a red or amber line to each shared file |
| **4 · Matrix** | the rows of a community × work table | the declared / expanded / seams columns | one column per lane or PR; red and amber cells; a cell rings its modules on a mini-map |

What each optimises, and what it gives up:

* **1 · Paint.**
  * Optimises: everything is visible at once, nothing moves, and it is the
    cheapest to build.
  * Gives up: design-system.md §1.6. This graph has 25 communities, so the
    eight hues drawn repeat, and they cannot be separated in greyscale. At
    2,000 modules, 37 rings in a hairball are not a territory anyone can
    read.
* **2 · Regions.**
  * Optimises: scale, and the plan-approval question "where does this issue
    land, and who is already there?".
  * Gives up: a territory file in a closed region is a count until it is
    opened. Regions also shift if KG2's communities drift between index
    runs. KG2's acceptance (Jaccard ≥ 0.9 across incremental runs) bounds
    that.
* **3 · Lens.**
  * Optimises: reading one territory and its collisions. Its size depends on
    the territory, not on the repository.
  * Gives up: while an issue is chosen, Structure stops showing the
    repository. It also means two Structure layouts to keep in step.
* **4 · Matrix.**
  * Optimises: a precise, accessible, DOM-testable answer, and the best
    phone story.
  * Gives up: the graph becomes secondary, and the owner asked for a graph
    layer. A matrix shows counts, not shape.

## 4. Costs

**Render time at 2,000 modules: measured.**

* **The test graph.** It has 2,034 modules and 9,733 edges: this repository's
  graph copied six times, with 3% random edges added between the copies,
  which gives 150 communities. The territory and the lanes are the example's.
* **The renderers.** The mock-up's own renderer functions drew it in headless
  Chromium 141, with software rendering, on a 6-vCPU container. The
  Network rows used vis-network 10.0.2 (GR3 pins 10.1.2) on the same graph,
  with positions given and physics off. They measure drawing and updating,
  not settling: graph-rendering.md §3.4 measured settling at 7,246 ms for
  2,000 modules.
* **The figures.** Each is the median of 5 runs, from building the markup to
  two animation frames later.

| | 1 · Paint | 2 · Regions | 3 · Lens | 4 · Matrix |
|---|---|---|---|---|
| Structure (SVG) | **133 ms**; 2,231 shapes + 9,733 edges | **33 ms**; 217 shapes | **33 ms**; 84 shapes | **100 ms** (table + full mini-map) |
| Network (vis-network): build | 918 ms | **126 ms** (184 nodes, 676 edges, pre-aggregated) | 918 ms | as 1 or 2 |
| Network: each overlay change | 215 ms | **31 ms** | 266 ms (dim all) | as 1 or 2 |
| Bundle, measured proxy (gz) | 0.5 kB | 1.4 kB | 1.8 kB | 1.4 kB |
| Bundle, estimate for the built TSX (gz) | +3 kB | +5 kB | +6 kB | +4 kB |
| New npm dependency | none | none | none | none |

* **Bundle proxy.** This is the gzipped, minified size of the mock-up's
  renderer for that variant. All four also share 2.1 kB of marks and overlap
  list. The estimate roughly doubles the total, for types, keyboard handling
  and tests.
* **vis-network's own `cluster()` must not be used for variant 2.** Called
  once per community at 2,034 modules, it took **41,187 ms** to fold 147
  communities. Handing vis-network the meta-graph already aggregated (the
  closed communities as nodes, plus the open ones' modules) built in 126 ms.
  So the aggregation belongs in `RepoGraphData.ts`, where GR3 already
  aggregates by directory, and both views draw its output.
* **Variant 1's Structure exceeds GR3's 100 ms per-task budget at 2,034
  modules** unless it is mounted in chunks, as GR3 does. GR3 lays out a view over
  `IN_PLACE_MAX` (120 modules) in a worker and mounts it 100 nodes per frame. Variant 4's full mini-map does too. An
  aggregated mini-map, as in variant 2, costs about 33 ms.
* **The GR budget still holds.** The graph work's budget (graph-rendering.md
  §7) is +10 kB gz in total, with no new runtime dependency. Every variant
  fits inside what GR3 leaves.

**Data each variant needs beyond KG3's three routes:**

* variant 1 needs a colour per community that stays stable across index runs;
* variant 2 needs `/communities` to carry the import count between each pair
  of communities, so that the meta-graph's edges are not recomputed in the
  browser;
* variants 3 and 4 need nothing more.

## 5. Questions for the owner
<a id="owner-questions"></a>

The same questions, with options, are in the run's `questions.json`.

1. **Which variant?** 1 Paint, 2 Regions, 3 Lens or 4 Matrix. They mix,
   because the marks are shared (§2). *Recommended: 2, with 4's
   per-community cards on the phone.*
2. **What does a 390px phone show?**
   * **A:** the full-size graph in a sideways scroller above the list, as
     drawn. It is never scaled to fit.
   * **B:** the list only, which is what graph-rendering.md §6 says the
     repository graph keeps at ≤560px.

   *Recommended: A.* The list is still there, so the rule holds, and the
   scroller adds the territory's shape at no layout cost.
3. **What opens by default when an issue is chosen (variant 2)?**
   * **a:** the communities holding a declared file (3 here);
   * **b:** also those holding a seam or a lane collision (9 here);
   * **c:** nothing opens until it is clicked.

   *Recommended: a.*
4. **How severe is an overlap?**
   * **As drawn:** a lane on a **declared** file is a collision (red); a lane
     anywhere else in the territory, seams included, is an overlap (amber).
   * **Stricter:** a seam counts as a collision too, because seams are where
     merges conflict.

   *Recommended: as drawn.* It matches CLAUDE.md's same-file rule and does
   not turn every UI lane that touches `api.ts` red.
5. **Where are the overlays reached from?**
   * **A:** only the repository's Graph tab, with an issue picker.
   * **B:** also a "Show on graph" link on the issue run's plan Context card,
     which opens Graph with the run's territory.

   *Recommended: B.* knowledge-graph.md §4.7 says the gain is "the owner
   sees where a plan will work before approving it". But B adds
   `RunIndex.tsx` to KG8's territory, which today is `RepoGraph.tsx`,
   `RepoGraphData.ts` and `api.ts`.

## 6. What was not verified

* **KG2's communities.** The real ones will differ from Louvain at
  resolution 3. The number of communities on a real repository drives
  variant 1's hue problem and variant 2's region count. 25 and 150 are what
  this page used.
* **KG3's territory.** Its expansion depth, and its seam rule (knowledge-graph.md
  §4.1 says fan-in plus co-change rank), are not settled. The page uses
  depth 1 and fan-in ≥ 47.
* **Real hardware.** The render times come from software-rendered headless
  Chromium in a container, not a laptop GPU. Read them as relative.
* **The bundle figures.** These are a proxy plus an estimate. No TSX was
  written.
* **The live state of #929.** It was read from its branch, not from GitHub:
  this container has no GitHub credentials.
