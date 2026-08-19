/*
 * Discograph viewer.
 *
 * Talks only to this server's own JSON API -- no database credentials reach the
 * page and no Cypher is built here. Layout is a damped mass-spring simulation
 * (see wake/physicsTick) that runs continuously rather than settling once and
 * stopping: repulsion keeps nodes apart, edges act as springs, and dragging a
 * node perturbs its neighbours and lets go with momentum, so the graph visibly
 * "boings" back into equilibrium instead of just snapping into place.
 *
 * The info panel is a constant fixture rather than a popup: it always shows
 * whichever node was hovered or tapped most recently (see showPanel), and
 * tapping is also what fetches a node's neighbourhood (see activate).
 */
'use strict';

const COLOURS = {
  Artist:  '#38bdf8',
  Group:   '#a78bfa',
  Release: '#fb7185',
  Label:   '#34d399',
  Unknown: '#94a3b8',
};

const REL_LABEL = {
  MEMBER_OF:   'member of',
  CREDITED:    'credited on',
  RELEASED_ON: 'released on',
  SUBLABEL:    'sublabel of',
};

// Live search (see fetchSuggestions): wait this long after the last keystroke
// before hitting /api/suggest, and don't bother firing below this many chars
// -- both keep a fast typist from spamming the endpoint on every keystroke.
const SUGGEST_DEBOUNCE_MS = 180;
const SUGGEST_MIN_CHARS = 2;

// Tuned for a viewport-ish 820x820 spread of a few hundred nodes -- see
// physicsTick. SPRING pulls edges toward an AREA_SIDE/nodeCount-derived `k`;
// REPULSE no longer references `k` at all, it's mass-based -- see below.
// DAMPING is what makes forces settle instead of oscillating forever, and
// MAX_AWAKE_FRAMES is a safety valve in case some pathological layout never
// quite dips under SLEEP_ENERGY.
const AREA_SIDE = 820;
// Repulsion is gravity-style: force = REPULSE_K * size_i * size_j / d^2, so
// it scales with a pair's own drawn sizes (see sizeFor), not node count or
// viewport. Two touching hubs (size ~26 each, mass 676) generate ~42x the
// force two touching leaves (size ~4 each, mass 16) do at the same distance
// -- hand-tuned so a typical mid-size pair feels roughly as strong as the
// old k-anchored scheme did. Bumping REPULSE_K adds force proportional to
// size_i * size_j, so the increase lands almost entirely on massy pairs
// (hub-hub, hub-leaf) and barely touches leaf-leaf ones -- raised from 45
// after hubs were still settling into knots, their springs (which pull
// toward the count-derived `k`, blind to node size) out-muscling the
// plateaued repulsion at close range.
const REPULSE_K = 90;
// How close (in drawn-radius units) a pair's repulsion is allowed to treat
// them as being, at minimum -- below this, the force stops climbing toward
// infinity as d -> 0 and plateaus instead. 0 would let coincident nodes
// generate unbounded force; 1 is roughly their combined radii; 1.8 leaves a
// generous margin between two touching hubs.
const SIZE_REPULSE_PAD = 1.8;
const SPRING_K = 0.03;
const DAMPING = 0.82;
const GRAVITY = 0.008;
const SLEEP_ENERGY = 0.0004;   // average per-node kinetic energy to go idle
const MAX_AWAKE_FRAMES = 900;  // ~15s at 60fps

// Keep in sync with styles.css's --bg -- the hover label's text is knocked
// out in this colour (see drawHoverLabel) so it reads as cut from the pill
// behind it rather than printed on it.
const PAGE_BG = '#0b1120';
// The white node rim's width/colour -- consumed by the border node program
// built in initRenderer, not a canvas stroke.
const OUTLINE_WIDTH = 2.5;
const OUTLINE_COLOR = '#ffffff';
// Opacity multiplier for nodes/edges on screen that aren't directly
// connected to whichever node is currently hovered -- see reduceNode/reduceEdge.
const FADE_ALPHA = 0.5;
// Edges have no per-edge colour of their own (see the addEdge call in
// merge()), so unlike nodes there's no stored value for reduceEdge to fade --
// these two are it, EDGE_COLOR_FADED being EDGE_COLOR's alpha scaled by
// FADE_ALPHA.
const EDGE_ALPHA = 0.35;
const EDGE_COLOR = `rgba(148,163,184,${EDGE_ALPHA})`;
const EDGE_COLOR_FADED = `rgba(148,163,184,${EDGE_ALPHA * FADE_ALPHA})`;

// Node size grows fast with degree up to about SIZE_KNEE connections, then
// flattens -- a hub with hundreds of connections should not dwarf everything
// else, but the difference between 1 and 8 connections should be obvious.
const SIZE_MIN = 4;
const SIZE_MAX = 26;
const SIZE_KNEE = 10;
// Reaches ~90% of the size range right at SIZE_KNEE, so most of the visible
// variation happens by degree ~10 and higher degrees taper off logarithmically.
const SIZE_SCALE = 0.9 * (SIZE_MAX - SIZE_MIN) / Math.log2(SIZE_KNEE + 1);

const $ = (id) => document.getElementById(id);

// '#rrggbb' -> 'rgba(r,g,b,alpha)', for fading a node's fill/border colour at
// render time (see reduceNode) without touching its stored `color` attribute.
function withAlpha(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

/*
 * Custom hover-label renderer, wired in via the `defaultDrawNodeHover`
 * setting in place of sigma's own: a pill in the node's own colour, with the
 * text knocked out in the page background colour so it reads as cut from the
 * pill rather than printed on it. The pill-around-node shape (an arc bridged
 * by two straight edges) is sigma's usual approach for wrapping a label box
 * around the node it labels.
 */
function drawHoverLabel(context, data, settings) {
  const size = settings.labelSize;
  const font = settings.labelFont;
  const weight = settings.labelWeight;
  const label = data.label;

  context.fillStyle = data.color || COLOURS.Unknown;

  if (!label) {
    context.beginPath();
    context.arc(data.x, data.y, data.size + 2, 0, Math.PI * 2);
    context.closePath();
    context.fill();
    return;
  }

  context.font = `${weight} ${size}px ${font}`;
  const PADDING = 3;
  const textWidth = context.measureText(label).width;
  const boxWidth = Math.round(textWidth + 8);
  const boxHeight = Math.round(size + 2 * PADDING);
  const radius = Math.max(data.size, size / 2) + PADDING;
  const angleRadian = Math.asin(Math.min(1, boxHeight / 2 / radius));
  const xDelta = Math.sqrt(Math.abs(radius * radius - (boxHeight / 2) ** 2));

  context.beginPath();
  context.moveTo(data.x + xDelta, data.y + boxHeight / 2);
  context.lineTo(data.x + radius + boxWidth, data.y + boxHeight / 2);
  context.lineTo(data.x + radius + boxWidth, data.y - boxHeight / 2);
  context.lineTo(data.x + xDelta, data.y - boxHeight / 2);
  context.arc(data.x, data.y, radius, angleRadian, -angleRadian);
  context.closePath();
  context.fill();

  context.fillStyle = PAGE_BG;
  context.fillText(label, data.x + data.size + 3, data.y + size / 3);
}

class Viewer {
  constructor() {
    this.graph = new graphology.Graph({ multi: false, type: 'undirected' });
    this.renderer = null;
    this.expanded = new Set();

    // Physics state, kept outside graphology since it is not rendered.
    this.velocity = new Map();   // nodeId -> {vx, vy}
    this.raf = null;
    this.awakeFrames = 0;
    this.pendingFit = false;     // camera should re-fit once the sim settles

    // Drag state.
    this.dragged = null;
    this.didDrag = false;
    this.dragVX = 0;
    this.dragVY = 0;

    // Live-search state.
    this.suggestTimer = null;
    this.suggestToken = null;   // stale-response guard, see fetchSuggestions
    this.activeSuggestions = [];
    this.highlightedSuggestion = -1;

    // Hover-fade state, read by reduceNode -- null when nothing is hovered.
    this.hoveredNode = null;
    this.hoveredNeighbours = null;

    this.initRenderer();
    this.initLegend();
    this.bindEvents();
    this.loadStats();
  }

  // ---------------------------------------------------------------- setup

  initRenderer() {
    // A white rim around every node, as a genuine WebGL node program rather
    // than a per-frame canvas overlay: sigma 3 bundles createNodeBorderProgram
    // into the core UMD build (window.Sigma.rendering), so no extra package is
    // needed -- unlike the standalone @sigma/node-border, which targets sigma 3
    // but ships no UMD build of its own and would need a bundler to vendor.
    // Concentric rings from the outside in: a fixed-width white ring, then the
    // node's own colour filling the rest.
    // The outer ring's colour is per-node (`attribute`, not `value`) so
    // reduceNode can fade it along with the fill on hover -- a `value` border
    // is one fixed uniform for every node in the draw call, with no per-node
    // override possible.
    const nodeProgram = Sigma.rendering.createNodeBorderProgram({
      borders: [
        { size: { value: OUTLINE_WIDTH, mode: 'pixels' }, color: { attribute: 'borderColor', defaultValue: OUTLINE_COLOR } },
        { size: { fill: true }, color: { attribute: 'color' } },
      ],
    });

    this.renderer = new Sigma(this.graph, $('graph'), {
      renderEdgeLabels: false,
      defaultNodeColor: COLOURS.Unknown,
      defaultEdgeColor: EDGE_COLOR,
      labelColor: { color: '#e2e8f0' },
      labelSize: 12,
      labelWeight: '500',
      labelDensity: 0.6,
      labelGridCellSize: 70,
      labelRenderedSizeThreshold: 9,
      minCameraRatio: 0.05,
      maxCameraRatio: 12,
      allowInvalidContainer: true,
      zIndex: true,
      nodeProgramClasses: { circle: nodeProgram },
      // Sigma 3 moved per-node-state drawing off the top-level `hoverRenderer`
      // setting and onto each node program; `defaultDrawNodeHover` is the
      // renderer-wide fallback used when a program (like the border one
      // above) doesn't set its own `drawHover`.
      defaultDrawNodeHover: drawHoverLabel,
      // Render-time only -- fades everything except the hovered node and its
      // direct neighbours, without touching the graph's own `color`
      // attribute (which the legend, panel etc. all still read at full
      // strength). See enterNode/leaveNode for what drives it.
      nodeReducer: (node, data) => this.reduceNode(node, data),
      edgeReducer: (edge, data) => this.reduceEdge(edge, data),
    });

    // The info panel is a constant fixture, not a popup -- it always shows
    // the most recently hovered (or tapped) node, rather than opening and
    // closing per interaction.
    this.renderer.on('enterNode', ({ node }) => {
      document.body.style.cursor = 'pointer';
      this.hoveredNode = node;
      this.hoveredNeighbours = new Set(this.graph.neighbors(node));
      this.setHighlight(node);
      this.showPanel(node);
    });
    this.renderer.on('leaveNode', () => {
      document.body.style.cursor = 'default';
      this.hoveredNode = null;
      this.hoveredNeighbours = null;
      this.setHighlight(null);
    });
    this.renderer.on('clickNode', ({ node }) => {
      // downNode/mousemovebody fire for a drag too; a real click never moved.
      if (this.didDrag) { this.didDrag = false; return; }
      this.activate(node);
    });
    this.renderer.on('clickStage', () => {
      this.hoveredNode = null;
      this.hoveredNeighbours = null;
      this.setHighlight(null);
    });

    this.bindDrag();
  }

  /*
   * Node dragging, following sigma's own drag-nodes pattern: `downNode` marks
   * the node being dragged, `mousemovebody` (fires even off-canvas) updates
   * its position and calls `preventSigmaDefault` so sigma's own click-drag
   * camera pan does not also fire, and plain `mouseup` releases it. Dragging
   * on empty space is untouched, so panning the background still works.
   */
  bindDrag() {
    const renderer = this.renderer;
    const captor = renderer.getMouseCaptor();

    renderer.on('downNode', ({ node }) => {
      this.dragged = node;
      this.didDrag = false;
      this.dragVX = 0;
      this.dragVY = 0;
      this.graph.setNodeAttribute(node, 'highlighted', true);
      this.wake();
    });

    captor.on('mousemovebody', (e) => {
      if (!this.dragged) return;
      this.didDrag = true;
      const pos = renderer.viewportToGraph(e);
      const prev = this.graph.getNodeAttributes(this.dragged);
      // Smoothed so the release velocity isn't dominated by one jumpy sample.
      this.dragVX = this.dragVX * 0.7 + (pos.x - prev.x) * 0.3;
      this.dragVY = this.dragVY * 0.7 + (pos.y - prev.y) * 0.3;
      this.graph.setNodeAttribute(this.dragged, 'x', pos.x);
      this.graph.setNodeAttribute(this.dragged, 'y', pos.y);
      e.preventSigmaDefault();
      e.original.preventDefault();
      e.original.stopPropagation();
    });

    captor.on('mouseup', () => {
      if (!this.dragged) return;
      this.graph.removeNodeAttribute(this.dragged, 'highlighted');
      // Hand the node its drag velocity so releasing it flings it free --
      // spring and repulsion then bounce it back into place, the "boing".
      this.velocity.set(this.dragged, { vx: this.dragVX * 5, vy: this.dragVY * 5 });
      this.dragged = null;
      this.wake();
    });

    // Pins the camera's fit bounds to their pre-drag extent, otherwise sigma
    // auto-fits to the bounding box and dragging a node outward makes the
    // whole view rescale under the finger.
    captor.on('mousedown', () => {
      if (!renderer.getCustomBBox()) renderer.setCustomBBox(renderer.getBBox());
    });
  }

  initLegend() {
    $('legend').innerHTML = Object.keys(COLOURS)
      .filter((k) => k !== 'Unknown')
      .map((k) => `<li><i style="background:${COLOURS[k]}"></i>${k}</li>`)
      .join('');
  }

  bindEvents() {
    $('search-form').addEventListener('submit', (e) => {
      e.preventDefault();
      this.hideSuggestions();
      const term = $('q').value.trim();
      if (term) {
        $('q').blur();           // dismisses the on-screen keyboard
        this.search(term);
      }
    });
    $('reset').addEventListener('click', () => this.clear());

    const input = $('q');
    input.addEventListener('input', () => this.onSearchInput());
    input.addEventListener('keydown', (e) => this.onSearchKeydown(e));
    // Delayed so a suggestion's own mousedown handler (which fires first,
    // see renderSuggestions) gets a chance to act before blur wipes the list.
    input.addEventListener('blur', () => setTimeout(() => this.hideSuggestions(), 150));
    document.addEventListener('click', (e) => {
      if (!e.target.closest('.search-box')) this.hideSuggestions();
    });
  }

  // ------------------------------------------------------------- fetching

  async api(path) {
    const res = await fetch(path, { headers: { Accept: 'application/json' } });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { detail = (await res.json()).error || detail; } catch (_) { /* keep */ }
      throw new Error(detail);
    }
    return res.json();
  }

  busy(on) {
    $('spinner').hidden = !on;
    $('go').disabled = on;
  }

  async loadStats() {
    // The server is up before the import finishes, so "no graph yet" is an
    // expected state rather than an error -- point at the progress page
    // instead of failing silently.
    const notReady = () => {
      $('counts').innerHTML = 'graph not loaded yet — <a href="/logs">see progress</a>';
      $('hint').innerHTML = '<p>The graph has not been built yet.</p>'
        + '<p class="muted"><a href="/logs">Watch the build progress</a></p>';
    };
    try {
      const stats = await this.api('/api/stats');
      const total = Object.values(stats).reduce((a, b) => a + b, 0);
      if (!total) return notReady();
      $('counts').textContent = Object.entries(stats)
        .map(([k, v]) => `${v.toLocaleString()} ${k}`).join(' · ');
    } catch (_) {
      notReady();
    }
  }

  // Search results already come back with first- and second-order neighbours
  // (see server.py's _expand_two_hop), so nothing further is fetched here.
  async search(term) {
    this.busy(true);
    try {
      const data = await this.api(`/api/search?q=${encodeURIComponent(term)}`);
      this.beginNewGraph();
      const added = this.merge(data);
      $('hint').hidden = added > 0;
      if (!added) this.toast(`Nothing found for “${term}”`);
      this.pendingFit = true;
      this.wake();
    } catch (err) {
      this.toast(err.message);
    } finally {
      this.busy(false);
    }
  }

  // Shared reset step between a committed search and picking a live-search
  // suggestion: both replace whatever's on screen rather than merging into it.
  beginNewGraph() {
    this.graph.clear();
    this.velocity.clear();
    this.expanded.clear();
    // A pinned custom bbox from a previous graph (see bindDrag) would
    // normalise this new one against stale bounds -- let sigma recompute.
    this.renderer.setCustomBBox(null);
    this.setHighlight(null);
  }

  // ---------------------------------------------------------- live search

  onSearchInput() {
    const term = $('q').value.trim();
    clearTimeout(this.suggestTimer);
    if (term.length < SUGGEST_MIN_CHARS) {
      this.hideSuggestions();
      return;
    }
    this.suggestTimer = setTimeout(() => this.fetchSuggestions(term), SUGGEST_DEBOUNCE_MS);
  }

  onSearchKeydown(e) {
    if (!this.activeSuggestions.length) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      this.highlightedSuggestion = Math.min(
        this.highlightedSuggestion + 1, this.activeSuggestions.length - 1,
      );
      this.updateHighlight();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      this.highlightedSuggestion = Math.max(this.highlightedSuggestion - 1, 0);
      this.updateHighlight();
    } else if (e.key === 'Enter' && this.highlightedSuggestion >= 0) {
      // No suggestion highlighted -> fall through to the form's own submit,
      // which runs the full-text `search` instead.
      e.preventDefault();
      this.selectSuggestion(this.highlightedSuggestion);
    } else if (e.key === 'Escape') {
      this.hideSuggestions();
    }
  }

  async fetchSuggestions(term) {
    // A newer keystroke's request can land before an older one's -- only the
    // most recently issued token is allowed to render its results.
    const token = Symbol();
    this.suggestToken = token;
    try {
      const data = await this.api(`/api/suggest?q=${encodeURIComponent(term)}`);
      if (this.suggestToken !== token) return;
      this.renderSuggestions(data.results || []);
    } catch (_) {
      if (this.suggestToken === token) this.hideSuggestions();
    }
  }

  renderSuggestions(results) {
    this.activeSuggestions = results;
    this.highlightedSuggestion = -1;
    const list = $('suggestions');
    if (!results.length) {
      this.hideSuggestions();
      return;
    }
    list.innerHTML = results.map((r, i) => `
      <li class="suggestion" data-index="${i}" role="option">
        <span class="suggestion-dot" style="background:${COLOURS[r.kind] || COLOURS.Unknown}"></span>
        <span class="suggestion-body">
          <span class="suggestion-title">${esc(r.title)}<span class="suggestion-kind">${esc(r.kind)}</span></span>
          <span class="suggestion-sub">${esc(this.subtitleFor(r))}</span>
        </span>
      </li>`).join('');
    // mousedown, not click: it fires before the input's blur handler would
    // otherwise wipe the list out from under the tap.
    list.querySelectorAll('.suggestion').forEach((el) => {
      el.addEventListener('mousedown', (e) => {
        e.preventDefault();
        this.selectSuggestion(Number(el.dataset.index));
      });
    });
    list.hidden = false;
    $('q').setAttribute('aria-expanded', 'true');
  }

  // Artist/Group/Label already carry their own bio (server.py's `suggest`
  // reuses the same `profile` property); a Release has none, so it's
  // described by what it's tied to instead.
  subtitleFor(r) {
    if (r.kind === 'Release') {
      const parts = [];
      if (r.artists.length) parts.push(r.artists.join(', '));
      if (r.labels.length) parts.push(r.labels.join(', '));
      if (r.year) parts.push(String(r.year));
      return parts.join(' · ') || 'Release';
    }
    return r.profile || r.kind;
  }

  updateHighlight() {
    const items = $('suggestions').querySelectorAll('.suggestion');
    items.forEach((el, i) => el.classList.toggle('active', i === this.highlightedSuggestion));
    items[this.highlightedSuggestion]?.scrollIntoView({ block: 'nearest' });
  }

  hideSuggestions() {
    const list = $('suggestions');
    list.hidden = true;
    list.innerHTML = '';
    this.activeSuggestions = [];
    this.highlightedSuggestion = -1;
    $('q').setAttribute('aria-expanded', 'false');
  }

  // Jumps straight to the chosen node -- its own elementId from the
  // full-text hit, not a fresh guess -- rather than re-running a full-text
  // search on its title.
  async selectSuggestion(index) {
    const picked = this.activeSuggestions[index];
    if (!picked) return;
    this.hideSuggestions();
    $('q').value = picked.title;
    this.busy(true);
    try {
      const data = await this.api(`/api/expand?id=${encodeURIComponent(picked.id)}`);
      this.beginNewGraph();
      this.merge(data);
      this.expanded.add(picked.id);
      $('hint').hidden = true;
      this.pendingFit = true;
      this.wake();
      if (this.graph.hasNode(picked.id)) this.showPanel(picked.id);
    } catch (err) {
      this.toast(err.message);
    } finally {
      this.busy(false);
    }
  }

  // -------------------------------------------------------------- drawing

  merge(data, origin) {
    let added = 0;
    const base = origin || { x: 0, y: 0 };

    (data.nodes || []).forEach((n) => {
      if (this.graph.hasNode(n.id)) return;
      const angle = Math.random() * Math.PI * 2;
      const radius = 8 + Math.random() * 40;
      this.graph.addNode(n.id, {
        ...n,
        // Sigma renders `label`, so the node type is kept as `kind`. Without
        // that split the panel shows the title where the type belongs.
        kind: n.label,
        label: n.title,
        x: base.x + Math.cos(angle) * radius,
        y: base.y + Math.sin(angle) * radius,
        size: this.sizeFor(n.degree),
        color: COLOURS[n.label] || COLOURS.Unknown,
      });
      this.velocity.set(n.id, { vx: 0, vy: 0 });
      added += 1;
    });

    (data.edges || []).forEach((e) => {
      if (!this.graph.hasNode(e.source) || !this.graph.hasNode(e.target)) return;
      if (e.source === e.target) return;
      if (this.graph.hasEdge(e.source, e.target)) return;
      this.graph.addEdge(e.source, e.target, { type_: e.type, size: 2 });
    });

    // Size reflects connections actually visible in the accumulated graph --
    // not just what this one fetch returned -- so a node keeps growing as
    // more of its neighbourhood gets pulled in across searches/selections.
    this.graph.forEachNode((node) => {
      this.graph.setNodeAttribute(node, 'size', this.sizeFor(this.graph.degree(node)));
    });

    return added;
  }

  sizeFor(degree) {
    const d = Math.max(0, degree || 0);
    return Math.min(SIZE_MAX, SIZE_MIN + Math.log2(d + 1) * SIZE_SCALE);
  }

  /*
   * Damped mass-spring simulation, run continuously in animation-frame ticks
   * rather than a bounded cooling schedule: repulsion between every pair of
   * nodes, spring attraction along edges pulling toward an ideal separation,
   * and velocity damping so perturbing the graph (adding nodes, dragging a
   * node) settles back down instead of jittering forever. It goes to sleep
   * once kinetic energy drops below SLEEP_ENERGY (or after MAX_AWAKE_FRAMES
   * regardless, as a safety net) and wake() restarts it on demand.
   *
   * The view is capped at a few hundred nodes, so O(n^2) repulsion is cheap
   * and Barnes-Hut is not worth a dependency.
   */
  wake() {
    this.awakeFrames = 0;
    if (this.raf) return;
    this.raf = requestAnimationFrame(() => this.physicsTick());
  }

  physicsTick() {
    const nodes = this.graph.nodes();
    if (!nodes.length) { this.raf = null; return; }

    const g = this.graph;
    const k = Math.sqrt((AREA_SIDE * AREA_SIDE) / nodes.length); // ideal separation, springs only -- see below
    const index = new Map(nodes.map((n, i) => [n, i]));
    const pos = nodes.map((n) => g.getNodeAttributes(n));
    const fx = new Float64Array(nodes.length);
    const fy = new Float64Array(nodes.length);

    // Repulsion: every pair pushes apart, gravity-style -- strength is the
    // product of the pair's own drawn sizes (see sizeFor), not `k`/viewport
    // density. A hub's mass grows independently as more of its neighbourhood
    // merges in, so two hubs now generate a much stronger field at any given
    // distance than two leaf nodes do (mass product runs from ~16 at two
    // leaves to ~676 at two maxed-out hubs). That's what stops hubs settling
    // into knots -- the old k-anchored target was the same for every pair
    // regardless of size, so it could not push big nodes apart any harder
    // than small ones.
    //
    // `floor` is the minimum clearance a pair is allowed to close to --
    // below it, the force is computed as if they were still `floor` apart
    // rather than climbing toward infinity, so two coincident hubs get a
    // strong but bounded shove instead of a velocity spike.
    for (let i = 0; i < nodes.length; i += 1) {
      for (let j = i + 1; j < nodes.length; j += 1) {
        let ax = pos[i].x - pos[j].x;
        let ay = pos[i].y - pos[j].y;
        let d2 = ax * ax + ay * ay;
        if (d2 < 0.01) {           // coincident nodes need a nudge to separate
          ax = Math.random() - 0.5;
          ay = Math.random() - 0.5;
          d2 = ax * ax + ay * ay || 0.01;
        }
        const d = Math.sqrt(d2);
        const floor = (pos[i].size + pos[j].size) * SIZE_REPULSE_PAD;
        const dEff = Math.max(d, floor);
        const f = REPULSE_K * (pos[i].size * pos[j].size) / (dEff * dEff);
        fx[i] += (ax / d) * f; fy[i] += (ay / d) * f;
        fx[j] -= (ax / d) * f; fy[j] -= (ay / d) * f;
      }
    }

    // Springs: edges pull toward the ideal separation `k`, both attracting
    // stretched-out neighbours and pushing back on ones squeezed together --
    // the latter is what makes a released node's neighbours bounce.
    g.forEachEdge((_e, _a, source, target) => {
      const i = index.get(source);
      const j = index.get(target);
      if (i === undefined || j === undefined) return;
      const ax = pos[j].x - pos[i].x;
      const ay = pos[j].y - pos[i].y;
      const d = Math.hypot(ax, ay) || 0.01;
      const f = (d - k) * SPRING_K;
      fx[i] += (ax / d) * f; fy[i] += (ay / d) * f;
      fx[j] -= (ax / d) * f; fy[j] -= (ay / d) * f;
    });

    const maxSpeed = k * 0.6;
    let energy = 0;

    for (let i = 0; i < nodes.length; i += 1) {
      const id = nodes[i];
      if (id === this.dragged) continue;   // kinematically pinned, not force-driven

      const v = this.velocity.get(id) || { vx: 0, vy: 0 };
      let vx = (v.vx + fx[i]) * DAMPING;
      let vy = (v.vy + fy[i]) * DAMPING;
      const speed = Math.hypot(vx, vy);
      if (speed > maxSpeed) { vx = (vx / speed) * maxSpeed; vy = (vy / speed) * maxSpeed; }

      let x = pos[i].x + vx;
      let y = pos[i].y + vy;
      x -= x * GRAVITY;            // weak gravity keeps components in frame
      y -= y * GRAVITY;

      g.setNodeAttribute(id, 'x', x);
      g.setNodeAttribute(id, 'y', y);
      this.velocity.set(id, { vx, vy });
      energy += vx * vx + vy * vy;
    }

    this.renderer.refresh();
    this.awakeFrames += 1;

    const settled = (energy / nodes.length) < SLEEP_ENERGY;
    if (this.dragged || (!settled && this.awakeFrames < MAX_AWAKE_FRAMES)) {
      this.raf = requestAnimationFrame(() => this.physicsTick());
    } else {
      this.raf = null;
      if (this.pendingFit) {
        this.pendingFit = false;
        // Pull back slightly once settled: sigma fits node centres, which
        // leaves labels on the outermost nodes clipped at the viewport edge.
        this.renderer.getCamera().animate(
          { x: 0.5, y: 0.5, ratio: 1.2 }, { duration: 320 },
        );
      }
    }
  }

  // ------------------------------------------------------------ selection

  /*
   * Tapping a node fetches its first- and second-order neighbourhood the
   * first time (there is no separate "Expand" step) and shows it in the
   * panel -- covering touch devices, which have no hover to drive showPanel.
   * Already-fetched nodes just update the panel.
   */
  async activate(nodeId) {
    if (!this.graph.hasNode(nodeId)) return;
    this.showPanel(nodeId);

    if (this.expanded.has(nodeId)) return;
    this.expanded.add(nodeId);
    this.busy(true);
    try {
      const origin = this.graph.getNodeAttributes(nodeId);
      const data = await this.api(`/api/expand?id=${encodeURIComponent(nodeId)}`);
      const added = this.merge(data, origin);
      if (added) this.wake();
      // Connection counts in the panel may have grown from the fetch.
      this.showPanel(nodeId);
    } catch (err) {
      this.expanded.delete(nodeId);
      this.toast(err.message);
    } finally {
      this.busy(false);
    }
  }

  // Ring highlight tracks true hover (or an active drag), independent of
  // which node the panel is currently showing.
  setHighlight(nodeId) {
    this.graph.forEachNode((n) => this.graph.setNodeAttribute(n, 'highlighted', n === nodeId));
    this.renderer.refresh();
  }

  /*
   * nodeReducer: on hover, fades every on-screen node that isn't the hovered
   * node itself or one of its direct neighbours, restoring full opacity as
   * soon as nothing is hovered (this.hoveredNode is null on rollout). Purely
   * a render-time override -- the graph's own `color` attribute is untouched,
   * so nothing else that reads it (legend, panel) needs to know about this.
   */
  reduceNode(node, data) {
    if (!this.hoveredNode || node === this.hoveredNode || this.hoveredNeighbours.has(node)) {
      return data;
    }
    return { ...data, color: withAlpha(data.color, FADE_ALPHA), borderColor: withAlpha(OUTLINE_COLOR, FADE_ALPHA) };
  }

  /*
   * edgeReducer counterpart to reduceNode: while a node is hovered, every
   * edge not touching it fades to EDGE_COLOR_FADED; edges into/out of the
   * hovered node are left alone.
   */
  reduceEdge(edge, data) {
    if (!this.hoveredNode) return data;
    const [source, target] = this.graph.extremities(edge);
    if (source === this.hoveredNode || target === this.hoveredNode) return data;
    return { ...data, color: EDGE_COLOR_FADED };
  }

  /*
   * The panel is a constant fixture -- it never hides -- and always shows
   * whichever node was hovered or tapped most recently, so this only ever
   * replaces its content, never its visibility.
   */
  showPanel(nodeId) {
    const a = this.graph.getNodeAttributes(nodeId);
    $('panel-kind').textContent = a.kind;
    $('panel-kind').style.background = COLOURS[a.kind] || COLOURS.Unknown;
    $('panel-title').textContent = a.title;

    const neighbours = this.graph.neighbors(nodeId).length;
    const rows = [];
    if (a.realname) rows.push(['Real name', a.realname]);
    if (a.year) rows.push(['Year', a.year]);
    // A Release has no artist of its own in the graph schema -- credit lives
    // one CREDITED hop away, on the Artist/Group node(s) attached to it. Only
    // whatever's already in the client-side graph is available here, same as
    // every other row below, so this is empty until that neighbour has been
    // fetched in (which search/expand do by default via the two-hop fetch).
    if (a.kind === 'Release') {
      const artists = [];
      this.graph.forEachEdge(nodeId, (_e, attrs, source, target, sourceAttrs, targetAttrs) => {
        if (attrs.type_ !== 'CREDITED') return;
        const other = source === nodeId ? targetAttrs : sourceAttrs;
        if (other && other.title) artists.push(other.title);
      });
      if (artists.length) rows.push(['Artist', artists.join(', ')]);
    }
    rows.push(['Shown connections', String(neighbours)]);

    const byType = {};
    this.graph.forEachEdge(nodeId, (_e, attrs) => {
      byType[attrs.type_] = (byType[attrs.type_] || 0) + 1;
    });
    Object.entries(byType).forEach(([t, c]) => {
      rows.push([REL_LABEL[t] || t.toLowerCase().replace(/_/g, ' '), String(c)]);
    });

    $('panel-meta').innerHTML = rows
      .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
    $('panel-profile').textContent = a.profile || '';
  }

  clear() {
    this.beginNewGraph();
    this.dragged = null;
    if (this.raf) { cancelAnimationFrame(this.raf); this.raf = null; }
    this.hideSuggestions();
    $('panel-kind').textContent = '';
    $('panel-kind').style.background = 'transparent';
    $('panel-title').textContent = 'Hover a node';
    $('panel-meta').innerHTML = '';
    $('panel-profile').textContent = 'Tap or hover any node to see its details here.';
    $('q').value = '';
    $('hint').hidden = false;
    this.renderer.refresh();
  }

  toast(message) {
    const el = document.createElement('div');
    el.className = 'toast';
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 3200);
  }
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

window.addEventListener('DOMContentLoaded', () => {
  window.viewer = new Viewer();
});
