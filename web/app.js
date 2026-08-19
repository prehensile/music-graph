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

// discogs.com's own URL path per kind -- Group has no separate URL space
// there, it rides on Artist's "/artist/" path same as a solo performer.
const DISCOGS_PATH = {
  Artist: 'artist',
  Group:  'artist',
  Release: 'release',
  Label:  'label',
};

// Builds the real discogs.com page for a node from its raw Discogs id (see
// server.py's discogsId, read off the artistId/groupId/releaseId/labelId
// property the import gave it) -- null if this kind has no Discogs page or
// the node came through without one.
function discogsUrl(kind, discogsId) {
  const path = DISCOGS_PATH[kind];
  if (!path || discogsId === null || discogsId === undefined) return null;
  return `https://www.discogs.com/${path}/${discogsId}`;
}

// Live search (see fetchSuggestions): wait this long after the last keystroke
// before hitting /api/suggest, and don't bother firing below this many chars
// -- both keep a fast typist from spamming the endpoint on every keystroke.
const SUGGEST_DEBOUNCE_MS = 180;
const SUGGEST_MIN_CHARS = 2;

// How long a clicked/tapped node's panel resists being overwritten by
// hovering past other nodes en route to it -- see stickPanel.
const STICKY_MS = 6000;

// Tuned for a viewport-ish 820x820 spread of a few hundred nodes -- see
// physicsTick. SPRING pulls edges toward an AREA_SIDE/nodeCount-derived `k`;
// REPULSE no longer references `k` at all, it's mass-based -- see below.
// DAMPING is what makes forces settle instead of oscillating forever, and
// MAX_AWAKE_FRAMES is a safety valve in case some pathological layout never
// quite dips under SLEEP_ENERGY.
//
// A mutable object, not bare consts, so the physics panel (see
// initPhysicsPanel) can retune it live from the browser without a reload --
// physicsTick reads PHYSICS.* every tick rather than closing over fixed
// values. DEFAULT_PHYSICS is the untouched original, kept around for the
// panel's own Reset button; PHYSICS starts as a shallow copy of it.
const DEFAULT_PHYSICS = {
  AREA_SIDE: 820,
  // Repulsion is gravity-style: force = REPULSE_K * size_i * size_j / d^2,
  // so it scales with a pair's own drawn sizes (see sizeFor), not node
  // count or viewport. Two touching hubs (size ~26 each, mass 676) generate
  // ~42x the force two touching leaves (size ~4 each, mass 16) do at the
  // same distance. Bumping REPULSE_K adds force proportional to
  // size_i * size_j, so the increase lands almost entirely on massy pairs
  // (hub-hub, hub-leaf) and barely touches leaf-leaf ones -- raised
  // 45 -> 90 after hubs were still settling into knots, their springs
  // (which pull toward the count-derived `k`, blind to node size)
  // out-muscling the plateaued repulsion at close range; raised again
  // 90 -> 160 once removing the layout's centring gravity entirely made
  // that same knotting visible again -- gravity's constant inward pull had
  // been partly masking under-strength repulsion by keeping everything
  // nearer the centre rather than letting hubs actually spread apart; and
  // to 800, alongside SPRING_K down at its own floor, from hand-tuning in
  // the physics panel once COLLISION_ITERATIONS existed to keep the result
  // overlap-free regardless -- with springs barely resisting it, repulsion
  // alone needs to be this strong to spread a knot out to begin with.
  REPULSE_K: 800,
  // How close (in drawn-radius units) a pair's repulsion is allowed to
  // treat them as being, at minimum -- below this, the force stops
  // climbing toward infinity as d -> 0 and plateaus instead. 0 would let
  // coincident nodes generate unbounded force. 1 (down from 1.8) means the
  // force only plateaus once a pair reaches literal contact -- the same
  // threshold the hard overlap-correction pass (see physicsTick) enforces
  // as a floor anyway, so the two no longer disagree about how close is
  // too close.
  SIZE_REPULSE_PAD: 1,
  SPRING_K: 0.005,
  DAMPING: 0.8,
  // DAMPING above removes the same *fraction* of a node's velocity every
  // tick regardless of how fast it's moving, so it does nothing extra
  // against a burst of speed specifically -- and a dense knot repelling
  // another dense knot produces exactly that: every node in one knot
  // pushes every node in the other (an O(n_a * n_b) sum, not one knot-vs-
  // knot force), so two knots drift apart under a much larger net force
  // than any single spring is pulling back with, and DAMPING alone barely
  // slows that down. DRIFT_FRICTION is a second, speed-scaled drag applied
  // on top: `1 / (1 + DRIFT_FRICTION * speed)` shrinks velocity by more
  // the faster a node is already moving, so it bites hard on that
  // aggregate-repulsion drift while barely touching the slow jostling
  // nodes do while settling into a knot -- unlike raising DAMPING, which
  // would slow both equally and make knots even slower to spread
  // internally.
  DRIFT_FRICTION: 0.03,
  // No centring gravity at all -- there used to be one (first a pull felt
  // at every radius, then narrowed to a dead-zone wall), and both versions
  // still visibly rounded the graph off into a circle over time. Any force
  // pointed at a fixed origin is isotropic in the same way repulsion is,
  // so the two together always have a single equilibrium shape -- a disc
  // -- regardless of the graph's actual edges; narrowing where it kicked
  // in just shrank the effect, it didn't remove it. Without it a component
  // that repulsion pushes away from the rest just drifts until repulsion
  // is too weak to matter and damping settles it there -- fine, since
  // sigma auto-fits the camera to whatever the bounding box ends up being
  // (see beginNewGraph) rather than assuming a fixed frame.
  SLEEP_ENERGY: 0.0004,   // average per-node kinetic energy to go idle
  MAX_AWAKE_FRAMES: 900,  // ~15s at 60fps
  // How many times the hard overlap-correction pass (see physicsTick)
  // re-scans every pair per tick, not just once. A single pass resolves one
  // pairwise overlap at a time and moves on -- fine for an isolated pair,
  // but a node wedged between several overlapping neighbours (several
  // leaves piled on one hub) gets shoved clear of one only to land back
  // inside another, since each pair is corrected independently and the
  // next pair's correction doesn't know about the one before it. Re-running
  // the same pass against the results of the last one is a standard
  // Gauss-Seidel relaxation: each iteration cleans up whatever the last one
  // left inconsistent, so a pile converges in a handful of iterations
  // within the one tick instead of leaking out over many frames (which is
  // what read as both "still overlapping" and "unstable/slow to settle").
  COLLISION_ITERATIONS: 4,
};
const PHYSICS = { ...DEFAULT_PHYSICS };

// Drives both the panel's slider rows and what Copy values writes out --
// one source of truth for which of PHYSICS's keys are user-tunable, in what
// range/step, and how many decimals are worth showing. Order here is
// display order.
const PHYSICS_PARAMS = [
  { key: 'REPULSE_K', label: 'Repulsion', min: 20, max: 1000, step: 5, decimals: 0 },
  { key: 'SIZE_REPULSE_PAD', label: 'Repulsion floor', min: 1, max: 3, step: 0.05, decimals: 2 },
  { key: 'SPRING_K', label: 'Spring strength', min: 0.005, max: 0.15, step: 0.005, decimals: 3 },
  { key: 'DAMPING', label: 'Damping', min: 0.5, max: 0.95, step: 0.01, decimals: 2 },
  { key: 'DRIFT_FRICTION', label: 'Drift friction', min: 0, max: 0.2, step: 0.005, decimals: 3 },
  { key: 'AREA_SIDE', label: 'Area side', min: 300, max: 2000, step: 20, decimals: 0 },
  { key: 'SLEEP_ENERGY', label: 'Sleep threshold', min: 0.0001, max: 0.005, step: 0.0001, decimals: 4 },
  { key: 'MAX_AWAKE_FRAMES', label: 'Max awake frames', min: 100, max: 3000, step: 50, decimals: 0 },
  { key: 'COLLISION_ITERATIONS', label: 'Collision iterations', min: 1, max: 10, step: 1, decimals: 0 },
];

// Keep in sync with styles.css's --bg -- the hover label's text is knocked
// out in this colour (see drawHoverLabel) so it reads as cut from the pill
// behind it rather than printed on it.
const PAGE_BG = '#0b1120';
// The white node rim's width/colour -- consumed by the border node program
// built in initRenderer, not a canvas stroke.
const OUTLINE_WIDTH = 2.5;
const OUTLINE_COLOR = '#ffffff';
const LABEL_COLOR = '#e2e8f0';
// An incomplete node's ring is dashed with this many dashes all the way
// round, regardless of the node's on-screen radius -- see drawIncompleteRings
// and the `expanded` node attribute (merge()). A fixed pixel dash length
// looked fine at typical sizes but fell apart zoomed out: a small node's
// much shorter circumference fit only a handful of dashes, chunky and
// sparse rather than a dashed ring. Scaling dash length off the radius
// instead keeps the count constant so it still reads as "dashed" at any size.
const INCOMPLETE_RING_DASH_COUNT = 12;
// Fraction of each dash+gap period that's dash rather than gap -- 4/7 is
// the old fixed [4, 3] pattern's own ratio, kept for the same look.
const INCOMPLETE_RING_DASH_RATIO = 4 / 7;
// How far nodes/labels/edges on screen that aren't directly connected to
// whichever node is currently hovered tween toward PAGE_BG -- see
// reduceNode/reduceEdge/lerpToBg. 0 = untouched, 1 = fully the background
// colour. Colour-tweened rather than alpha-faded: an alpha-faded node is
// genuinely translucent, so whatever happens to be drawn behind it at that
// moment -- an edge, another node -- shows through it, which reads as a
// rendering glitch rather than "dimmed". A solid, darker colour has no such
// dependency on what's underneath.
// The transition toward and away from it is animated (see animateFade),
// eased FADE_EASE of the remaining distance per frame; FADE_SNAP is how
// close counts as "arrived", to stop the animation loop rather than
// chasing an imperceptible remainder forever.
const FADE_MIX = 0.82;
const FADE_EASE = 0.25;
const FADE_SNAP = 0.004;
// Edges have no per-edge colour of their own (see the addEdge call in
// merge()) -- EDGE_RGB/EDGE_ALPHA are the base grey both the default colour
// (EDGE_COLOR, built below) and reduceEdge's tweened one are derived from.
// Edges keep their normal alpha even while faded -- translucent edges are
// the intended baseline look (see EDGE_COLOR), not a side effect of fading
// -- only their colour tweens toward the background.
const EDGE_RGB = [148, 163, 184];
const EDGE_ALPHA = 0.35;

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

// Sigma's WebGL contexts are all set up with gl.blendFunc(gl.ONE,
// gl.ONE_MINUS_SRC_ALPHA) -- standard premultiplied-alpha compositing -- but
// the node/edge/border shaders write out gl_FragColor as plain, un-multiplied
// colour. Fed a colour at less than full alpha, that combination adds the
// *full-brightness* colour on top of only (1 - alpha) of the background,
// rather than fading it toward the background: against this app's near-black
// bg that overshoot reads as barely-dimmed-at-all. Baking the alpha into
// r/g/b ourselves -- producing genuinely premultiplied output -- is what the
// blend func was actually expecting, and makes alpha behave as alpha. Only
// EDGE_COLOR (below) still needs this -- the hover fade itself no longer
// varies alpha at all, see FADE_MIX.
function rgbaPremultiplied(r, g, b, alpha) {
  return `rgba(${Math.round(r * alpha)},${Math.round(g * alpha)},${Math.round(b * alpha)},${alpha})`;
}

function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

const PAGE_BG_RGB = hexToRgb(PAGE_BG);

// Tweens `hex` toward the page background by fraction `t` (0..1) and hands
// back a fully-opaque colour -- see FADE_MIX for why this replaces alpha for
// the hover fade. `rgb()` without an alpha component parses as opaque in
// both the WebGL colour parser and canvas fillStyle, so one helper covers
// reduceNode's fill/border and labelColor alike.
function lerpToBg(hex, t) {
  const [r, g, b] = hexToRgb(hex);
  const [br, bg, bb] = PAGE_BG_RGB;
  return `rgb(${Math.round(r + (br - r) * t)},${Math.round(g + (bg - g) * t)},${Math.round(b + (bb - b) * t)})`;
}

const EDGE_COLOR = rgbaPremultiplied(...EDGE_RGB, EDGE_ALPHA);

// reduceEdge's faded variant: EDGE_RGB tweened toward the background by `t`,
// same as lerpToBg, but at EDGE_ALPHA rather than fully opaque -- edges stay
// translucent at their normal baseline throughout, see EDGE_ALPHA above --
// so still needs premultiplying.
function edgeColorAt(t) {
  const [br, bg, bb] = PAGE_BG_RGB;
  const [r, g, b] = EDGE_RGB;
  return rgbaPremultiplied(r + (br - r) * t, g + (bg - g) * t, b + (bb - b) * t, EDGE_ALPHA);
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

    // Hover-fade state. hoveredNode/hoveredNeighbours are what's exempt from
    // fading; fadeCurrent is the live, animated tween-toward-background
    // fraction applied to everything else (see animateFade/lerpToBg),
    // easing toward fadeTarget (0 = normal, FADE_MIX = fully faded) rather
    // than snapping. hoveredNode/hoveredNeighbours stay set through a
    // roll-out (fadeTarget back to 0) until fadeCurrent actually gets there
    // -- nulling them the instant the pointer leaves would leave nothing
    // exempt while that transition was still playing.
    this.hoveredNode = null;
    this.hoveredNeighbours = null;
    // Set instead of hoveredNode while a legend key is rolled over -- see
    // initLegend. reduceNode/reduceEdge treat every node of this kind as
    // exempt from the fade, same roll-out-survives-until-fadeCurrent-is-0
    // rule as hoveredNode above.
    this.highlightKind = null;
    this.fadeCurrent = 0;
    this.fadeTarget = 0;
    this.fadeRaf = null;

    // A tapped/clicked node's panel content resists being overwritten by
    // hovering past other nodes on the way to it -- e.g. its Discogs link,
    // which sits off-canvas -- for STICKY_MS after the click. See
    // stickPanel/showPanel.
    this.stickyNode = null;
    this.stickyTimer = null;

    this.initRenderer();
    this.initLegend();
    this.initPhysicsPanel();
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
      // `attribute` lets reduceNode override a faded node's label colour the
      // same way it overrides fill/border -- see lerpToBg.
      labelColor: { attribute: 'labelColor', color: LABEL_COLOR },
      labelSize: 12,
      labelWeight: '500',
      labelDensity: 0.6,
      labelGridCellSize: 70,
      labelRenderedSizeThreshold: 9,
      minCameraRatio: 0.05,
      maxCameraRatio: 12,
      // Sigma's default (`itemSizesReference: 'screen'`) keeps a node's
      // rendered radius roughly fixed in actual screen pixels regardless of
      // zoom -- position scales with the camera, size doesn't. physicsTick
      // has never worked that way: it computes repulsion/collision purely
      // in graph-space units, comparing `size` directly against x/y deltas
      // as if they already lived in the same space (see SIZE_REPULSE_PAD,
      // and the hard overlap-correction pass's `minSep`). Under the
      // default reference those two notions of "size" only coincide by
      // accident at whatever one camera ratio makes scaleSize(s) == s --
      // at any other zoom (i.e. basically always, once the camera auto-
      // fits to a real graph's bounding box), a pair the physics considers
      // exactly non-overlapping can still be drawn overlapping on screen,
      // which no amount of tuning REPULSE_K/collision iterations can fix
      // because it's a unit mismatch, not a strength one. 'positions'
      // makes size scale with the camera exactly like x/y do, so a
      // graph-space non-overlap guarantee is a screen-space one too, at
      // every zoom level.
      itemSizesReference: 'positions',
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

    // A second canvas layer, purely for the dashed "incomplete neighbourhood"
    // ring (see drawIncompleteRings) -- the border node program only draws a
    // solid ring, and WebGL has no notion of a dash pattern short of writing
    // a custom shader. pointer-events stays off so this is strictly visual
    // and never steals a hover/click/drag from sigma's own mouse-capture
    // layer underneath it. `beforeLayer: 'labels'` is load-bearing: sigma's
    // own layer order is edges, edgeLabels, nodes, labels, hovers, hoverNodes,
    // mouse, and createCanvasContext with no position just appends -- on top
    // of labels and hovers as well as nodes. That put the dashed ring above
    // every node's label instead of only above the (solid-ringed) node it
    // stands in for. Inserted before 'labels' instead, it sits directly on
    // top of the nodes layer, matching where the solid ring it replaces
    // actually lives.
    this.renderer.createCanvasContext('incompleteRing', {
      style: { pointerEvents: 'none' },
      beforeLayer: 'labels',
    });
    this.ringCtx = this.renderer.canvasContexts.incompleteRing;
    // resize() sizes (and devicePixelRatio-scales) every layer's actual
    // canvas element -- but bails out immediately unless the container's
    // own size just changed, which it hasn't here. Called only from sigma's
    // own constructor before this layer existed, our canvas would otherwise
    // sit at the browser's default 300x150 backing store forever, i.e.
    // never actually visible. `true` forces it through regardless.
    this.renderer.resize(true);
    this.renderer.on('afterRender', () => this.drawIncompleteRings());

    // The info panel is a constant fixture, not a popup -- it always shows
    // the most recently hovered (or tapped) node, rather than opening and
    // closing per interaction.
    this.renderer.on('enterNode', ({ node }) => {
      document.body.style.cursor = 'pointer';
      this.hoveredNode = node;
      this.hoveredNeighbours = new Set(this.graph.neighbors(node));
      this.fadeTarget = FADE_MIX;
      this.animateFade();
      this.setHighlight(node);
      // A sticky node (see stickPanel) holds the panel through a hover
      // elsewhere -- e.g. crossing another node while the pointer heads for
      // the panel's own Discogs link, off-canvas -- except hovering the
      // sticky node itself, which is just the content it already shows.
      if (!this.stickyNode || this.stickyNode === node) this.showPanel(node);
    });
    this.renderer.on('leaveNode', () => {
      document.body.style.cursor = 'default';
      this.fadeTarget = 0;
      this.animateFade();
      this.setHighlight(null);
    });
    this.renderer.on('clickNode', ({ node }) => {
      // downNode/mousemovebody fire for a drag too; a real click never moved.
      if (this.didDrag) { this.didDrag = false; return; }
      this.activate(node);
    });
    this.renderer.on('clickStage', () => {
      this.fadeTarget = 0;
      this.animateFade();
      this.setHighlight(null);
      // A deliberate click on empty space reads as "done with that node" --
      // release the sticky hold early rather than making them wait it out.
      if (this.stickyTimer) { clearTimeout(this.stickyTimer); this.stickyTimer = null; }
      this.stickyNode = null;
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

  // Rolling over a key reuses enterNode/leaveNode's own fade tween (see
  // reduceNode/reduceEdge) rather than a separate effect: it just drives
  // highlightKind instead of hoveredNode, so every node of that kind is
  // exempt from the fade in place of a single node and its neighbours.
  initLegend() {
    $('legend').innerHTML = Object.keys(COLOURS)
      .filter((k) => k !== 'Unknown')
      .map((k) => `<li data-kind="${k}"><i style="background:${COLOURS[k]}"></i>${k}</li>`)
      .join('');
    $('legend').querySelectorAll('li').forEach((li) => {
      const kind = li.dataset.kind;
      li.addEventListener('mouseenter', () => {
        this.highlightKind = kind;
        this.fadeTarget = FADE_MIX;
        this.animateFade();
      });
      li.addEventListener('mouseleave', () => {
        // highlightKind itself stays set until fadeCurrent actually reaches
        // 0 -- see animateFade -- so the key's own nodes don't jump back to
        // "faded" mid roll-out.
        this.fadeTarget = 0;
        this.animateFade();
      });
    });
  }

  /*
   * A live tuning panel over PHYSICS, built from PHYSICS_PARAMS rather than
   * one hardcoded row per constant -- adding a param to the array is enough
   * to get it a slider. Each row writes straight into PHYSICS on 'input' and
   * wakes the simulation, so a drag is felt immediately even if the layout
   * had already gone to sleep. Hidden by default; toggled from the footer's
   * "Physics" button. Reset restores DEFAULT_PHYSICS; Copy values writes the
   * live PHYSICS out as a paste-able object literal, for taking whatever was
   * hand-tuned here back into the source as the new defaults.
   */
  initPhysicsPanel() {
    $('physics-controls').innerHTML = PHYSICS_PARAMS
      .map((p) => `
        <label class="physics-row" data-key="${p.key}">
          <span class="physics-row-head">
            <span>${p.label}</span>
            <span class="physics-value">${PHYSICS[p.key].toFixed(p.decimals)}</span>
          </span>
          <input type="range" min="${p.min}" max="${p.max}" step="${p.step}" value="${PHYSICS[p.key]}">
        </label>
      `)
      .join('');

    PHYSICS_PARAMS.forEach((p) => {
      const row = $('physics-controls').querySelector(`[data-key="${p.key}"]`);
      const input = row.querySelector('input');
      const value = row.querySelector('.physics-value');
      input.addEventListener('input', () => {
        PHYSICS[p.key] = Number(input.value);
        value.textContent = PHYSICS[p.key].toFixed(p.decimals);
        this.wake();
      });
    });

    const panel = $('physics-panel');
    $('physics-toggle').addEventListener('click', () => { panel.hidden = !panel.hidden; });
    $('physics-close').addEventListener('click', () => { panel.hidden = true; });

    $('physics-reset').addEventListener('click', () => {
      Object.assign(PHYSICS, DEFAULT_PHYSICS);
      PHYSICS_PARAMS.forEach((p) => {
        const row = $('physics-controls').querySelector(`[data-key="${p.key}"]`);
        row.querySelector('input').value = PHYSICS[p.key];
        row.querySelector('.physics-value').textContent = PHYSICS[p.key].toFixed(p.decimals);
      });
      this.wake();
    });

    $('physics-copy').addEventListener('click', () => {
      const body = PHYSICS_PARAMS.map((p) => `  ${p.key}: ${PHYSICS[p.key]},`).join('\n');
      navigator.clipboard.writeText(`{\n${body}\n}`)
        .then(() => this.toast('Physics values copied'))
        .catch(() => this.toast('Copy failed -- clipboard unavailable'));
    });
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
    // Snap the fade state back rather than animating it: a search can land
    // mid-fade (hovering, then pressing Enter) with the pointer nowhere near
    // the canvas afterwards, so nothing would ever fire leaveNode to
    // un-fade the new graph -- and hoveredNode/hoveredNeighbours would be
    // stale ids from the graph just cleared regardless.
    if (this.fadeRaf) { cancelAnimationFrame(this.fadeRaf); this.fadeRaf = null; }
    this.hoveredNode = null;
    this.hoveredNeighbours = null;
    this.highlightKind = null;
    this.fadeCurrent = 0;
    this.fadeTarget = 0;
    this.setHighlight(null);
    // Same reasoning as the fade state above -- a sticky hold from the
    // previous graph has nothing valid left to hold onto.
    if (this.stickyTimer) { clearTimeout(this.stickyTimer); this.stickyTimer = null; }
    this.stickyNode = null;
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
      // `expanded` (server-set in _expand_two_hop) means this node's own
      // neighbourhood was actually queried -- true for a search/expand
      // target and its direct neighbours, false for a node that only
      // arrived because it happened to connect to one of those. Mirrored
      // into this.expanded so a later tap on an already-complete node
      // (e.g. one of the hop1 set) doesn't needlessly refetch it -- see
      // activate(). Checked even for a node already on screen: it may have
      // been added earlier as incomplete (hop2-only) and only now, via a
      // different fetch, found out its own neighbourhood is complete.
      if (n.expanded) this.expanded.add(n.id);
      if (this.graph.hasNode(n.id)) {
        if (n.expanded) this.graph.setNodeAttribute(n.id, 'expanded', true);
        return;
      }
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
    const k = Math.sqrt((PHYSICS.AREA_SIDE * PHYSICS.AREA_SIDE) / nodes.length); // ideal separation, springs only -- see below
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
        const floor = (pos[i].size + pos[j].size) * PHYSICS.SIZE_REPULSE_PAD;
        const dEff = Math.max(d, floor);
        const f = PHYSICS.REPULSE_K * (pos[i].size * pos[j].size) / (dEff * dEff);
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
      const f = (d - k) * PHYSICS.SPRING_K;
      fx[i] += (ax / d) * f; fy[i] += (ay / d) * f;
      fx[j] -= (ax / d) * f; fy[j] -= (ay / d) * f;
    });

    const maxSpeed = k * 0.6;
    const newX = new Float64Array(nodes.length);
    const newY = new Float64Array(nodes.length);
    const preX = new Float64Array(nodes.length);  // pre-collision position, to recover the collision's own displacement below
    const preY = new Float64Array(nodes.length);
    const velX = new Float64Array(nodes.length);  // force-integrated velocity, before collision feedback
    const velY = new Float64Array(nodes.length);

    for (let i = 0; i < nodes.length; i += 1) {
      const id = nodes[i];
      if (id === this.dragged) { newX[i] = pos[i].x; newY[i] = pos[i].y; continue; }

      const v = this.velocity.get(id) || { vx: 0, vy: 0 };
      let vx = (v.vx + fx[i]) * PHYSICS.DAMPING;
      let vy = (v.vy + fy[i]) * PHYSICS.DAMPING;

      // Speed-scaled drag, on top of DAMPING's flat per-tick loss -- see
      // DRIFT_FRICTION. Applied before the maxSpeed clamp below so it acts
      // on the node's actual velocity, not the clamped one.
      const rawSpeed = Math.hypot(vx, vy);
      if (rawSpeed > 0 && PHYSICS.DRIFT_FRICTION > 0) {
        const drag = 1 / (1 + PHYSICS.DRIFT_FRICTION * rawSpeed);
        vx *= drag;
        vy *= drag;
      }

      const speed = Math.hypot(vx, vy);
      if (speed > maxSpeed) { vx = (vx / speed) * maxSpeed; vy = (vy / speed) * maxSpeed; }

      velX[i] = vx;
      velY[i] = vy;
      newX[i] = pos[i].x + vx;
      newY[i] = pos[i].y + vy;
    }
    preX.set(newX);
    preY.set(newY);

    /*
     * Hard overlap correction -- a second, purely positional pass, separate
     * from the force integration above and run to convergence rather than
     * once. Repulsion only ever pushes nodes apart as a *force*; it settles
     * wherever that force nets to ~0, which is not the same guarantee as
     * "not overlapping", and two things above can each independently stall
     * it short of that: maxSpeed caps every node's per-tick displacement at
     * k*0.6 regardless of how much force is behind it, and DRIFT_FRICTION
     * deliberately suppresses the exact kind of high per-tick speed a
     * strong close-range shove produces (that's its job -- see
     * DRIFT_FRICTION -- it just also fights this). Past whichever of those
     * two saturates first, no amount of REPULSE_K does anything more per
     * tick, so overlap can persist no matter how the spring/repulsion
     * knobs are tuned.
     *
     * Each treats every node as a rigid, non-overlapping disc: any pair
     * still closer than the sum of their drawn radii gets shoved apart by
     * exactly the overlap, split between them, independent of velocity. A
     * single sweep only ever resolves each pair in isolation, though -- a
     * node wedged between several overlapping neighbours (several leaves
     * piled on one hub) gets pushed clear of one only to land inside
     * another, since each pair's correction has no idea about the next
     * one's. COLLISION_ITERATIONS re-runs the sweep against the previous
     * sweep's own output -- standard Gauss-Seidel relaxation -- so a whole
     * pile converges within the one tick instead of leaking out a little
     * per frame over many (which is what read as both persistent overlap
     * and jittery, slow-to-settle motion).
     */
    let overlapping = false;
    for (let iter = 0; iter < PHYSICS.COLLISION_ITERATIONS; iter += 1) {
      let movedThisIter = false;
      for (let i = 0; i < nodes.length; i += 1) {
        for (let j = i + 1; j < nodes.length; j += 1) {
          let ax = newX[i] - newX[j];
          let ay = newY[i] - newY[j];
          const minSep = pos[i].size + pos[j].size;
          let d2 = ax * ax + ay * ay;
          if (d2 >= minSep * minSep) continue;
          overlapping = true;
          movedThisIter = true;
          if (d2 < 0.01) {          // coincident nodes need a nudge to separate
            ax = Math.random() - 0.5;
            ay = Math.random() - 0.5;
            d2 = ax * ax + ay * ay || 0.01;
          }
          const d = Math.sqrt(d2);
          const nx = ax / d;
          const ny = ay / d;
          const iPinned = nodes[i] === this.dragged;
          const jPinned = nodes[j] === this.dragged;
          // A pinned node doesn't move for this either -- the other one
          // absorbs the full correction instead of the usual half each.
          const push = minSep - d;
          if (!iPinned) {
            const share = jPinned ? push : push / 2;
            newX[i] += nx * share; newY[i] += ny * share;
          }
          if (!jPinned) {
            const share = iPinned ? push : push / 2;
            newX[j] -= nx * share; newY[j] -= ny * share;
          }
        }
      }
      if (!movedThisIter) break;   // already fully separated -- no need to spend the remaining iterations
    }

    // Fold the collision pass's own displacement back into velocity, rather
    // than only teleporting position -- otherwise next tick's force
    // integration starts from a velocity that no longer matches where the
    // node actually is, which is exactly what reads as jitter (a corrected
    // node "forgets" it was just shoved and immediately drifts back
    // in-frame under old momentum).
    let energy = 0;
    for (let i = 0; i < nodes.length; i += 1) {
      const id = nodes[i];
      if (id === this.dragged) continue;   // kinematically pinned, not force-driven
      const vx = velX[i] + (newX[i] - preX[i]);
      const vy = velY[i] + (newY[i] - preY[i]);
      this.velocity.set(id, { vx, vy });
      energy += vx * vx + vy * vy;
      g.setNodeAttribute(id, 'x', newX[i]);
      g.setNodeAttribute(id, 'y', newY[i]);
    }

    this.renderer.refresh();
    this.awakeFrames += 1;

    const settled = !overlapping && (energy / nodes.length) < PHYSICS.SLEEP_ENERGY;
    if (this.dragged || (!settled && this.awakeFrames < PHYSICS.MAX_AWAKE_FRAMES)) {
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
    this.stickPanel(nodeId);
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

  // Marks nodeId as holding the panel for STICKY_MS -- see enterNode, which
  // is what actually withholds a hover-driven update while a node is
  // sticky. A second click (this node or another) restarts the window
  // rather than stacking; expiring just releases the hold; whatever's
  // hovered (or still this node) at that point carries on updating the
  // panel normally.
  stickPanel(nodeId) {
    this.stickyNode = nodeId;
    if (this.stickyTimer) clearTimeout(this.stickyTimer);
    this.stickyTimer = setTimeout(() => {
      this.stickyNode = null;
      this.stickyTimer = null;
    }, STICKY_MS);
  }

  /*
   * Eases fadeCurrent toward fadeTarget every animation frame and refreshes
   * the renderer so reduceNode/reduceEdge pick up the new value each time --
   * this is what makes the hover fade a transition rather than an instant
   * snap. Re-targeting mid-flight (hovering a new node before the last
   * one's roll-out finished) just changes fadeTarget under a loop that's
   * already running, so it picks up smoothly from wherever fadeCurrent
   * currently is rather than restarting. Only once fadeCurrent actually
   * settles back at 0 (fully rolled out) are hoveredNode/hoveredNeighbours
   * cleared -- see the constructor comment for why.
   */
  animateFade() {
    if (this.fadeRaf) return;
    const step = () => {
      const diff = this.fadeTarget - this.fadeCurrent;
      if (Math.abs(diff) < FADE_SNAP) {
        this.fadeCurrent = this.fadeTarget;
        this.fadeRaf = null;
        if (this.fadeTarget === 0) {
          this.hoveredNode = null;
          this.hoveredNeighbours = null;
          this.highlightKind = null;
        }
        this.renderer.refresh();
        return;
      }
      this.fadeCurrent += diff * FADE_EASE;
      this.renderer.refresh();
      this.fadeRaf = requestAnimationFrame(step);
    };
    this.fadeRaf = requestAnimationFrame(step);
  }

  /*
   * Draws a dashed ring over every node whose neighbourhood isn't complete
   * (`!expanded`, see merge()), on the extra canvas layer set up in
   * initRenderer. Runs on sigma's own `afterRender` event rather than being
   * folded into the border node program because WebGL has no dash pattern
   * short of a custom shader, and because sigma only calls a node program's
   * own `drawLabel`/`drawHover` for the hovered node or a label-density-
   * selected subset -- not for every rendered node every frame, which is
   * what a persistent per-node indicator needs.
   *
   * getNodeDisplayData/framedGraphToViewport/scaleSize are the same calls
   * sigma's own label/hover drawing makes internally to go from a node's
   * graph-space position and size to on-screen pixels, so this lines up
   * with the WebGL-drawn node under it without reimplementing the camera
   * math by hand.
   */
  drawIncompleteRings() {
    const ctx = this.ringCtx;
    const { width, height } = this.renderer.getDimensions();
    ctx.clearRect(0, 0, width, height);
    ctx.lineWidth = OUTLINE_WIDTH;
    this.graph.forEachNode((node, attrs) => {
      if (attrs.expanded) return;
      const display = this.renderer.getNodeDisplayData(node);
      if (!display) return; // culled -- off-screen or otherwise not drawn this frame
      const { x, y } = this.renderer.framedGraphToViewport(display);
      // Half the ring's own width inward, so the dashed stroke (centred on
      // its path by canvas convention) spans the same annulus the WebGL
      // ring would have -- see reduceNode's `size` -> outer-edge relationship.
      const r = this.renderer.scaleSize(display.size) - OUTLINE_WIDTH / 2;
      // canvas arc() throws on a negative radius -- reachable zoomed far
      // enough out that a small node's scaled size shrinks past the
      // border inset. Nothing meaningful to draw at that point anyway.
      if (r <= 0) return;
      // Dash length off this node's own circumference (see
      // INCOMPLETE_RING_DASH_COUNT) rather than a fixed pixel length, so a
      // tiny zoomed-out node still reads as dashed instead of a handful of
      // chunky marks.
      const period = (2 * Math.PI * r) / INCOMPLETE_RING_DASH_COUNT;
      ctx.setLineDash([period * INCOMPLETE_RING_DASH_RATIO, period * (1 - INCOMPLETE_RING_DASH_RATIO)]);
      ctx.strokeStyle = display.ringColor || OUTLINE_COLOR;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.stroke();
    });
  }

  /*
   * nodeReducer, doing two unrelated things at once (both cheap, so one pass
   * covers both rather than composing two reducers):
   *
   * 1. Tweens every on-screen node that isn't exempt (the hovered node and
   *    its direct neighbours, or every node of the rolled-over legend key's
   *    kind -- see the `exempt` check below) toward the background colour,
   *    at whatever point animateFade's transition currently is (see
   *    FADE_MIX for why colour, not alpha).
   * 2. Hides the WebGL border ring on an incomplete node (`!data.expanded`,
   *    see merge()) by colouring it to match the fill, and stashes what the
   *    ring *would* be in `ringColor` for drawIncompleteRings to read back
   *    via getNodeDisplayData -- that's what actually draws its ring, as a
   *    dashed one, on the canvas overlay created in initRenderer. A ring
   *    that's solid-but-hidden underneath a dashed one drawn on top would
   *    just show through the gaps, so the WebGL ring has to genuinely not
   *    be there rather than merely be covered.
   *
   * Purely a render-time override either way -- the graph's own `color`/
   * `label`/`expanded` attributes are untouched, so nothing else that reads
   * them (legend, panel, drawIncompleteRings) needs to know about this.
   */
  reduceNode(node, data) {
    // Exempt from the fade: the hovered node and its direct neighbours
    // normally, or -- while a legend key is rolled over (see initLegend) --
    // every node of that key's kind instead.
    const exempt = this.highlightKind
      ? data.kind === this.highlightKind
      : node === this.hoveredNode || this.hoveredNeighbours?.has(node);
    const faded = this.fadeCurrent > 0 && !exempt;
    const incomplete = !data.expanded;
    if (!faded && !incomplete) return data;

    const ring = faded ? lerpToBg(OUTLINE_COLOR, this.fadeCurrent) : OUTLINE_COLOR;
    const out = {
      ...data,
      color: faded ? lerpToBg(data.color, this.fadeCurrent) : data.color,
      ringColor: ring,
    };
    out.borderColor = incomplete ? out.color : ring;
    if (faded) out.labelColor = lerpToBg(LABEL_COLOR, this.fadeCurrent);
    return out;
  }

  /*
   * edgeReducer counterpart to reduceNode: every edge not touching the
   * hovered node tweens the same way; edges into/out of it are left alone.
   */
  reduceEdge(edge, data) {
    if (this.fadeCurrent <= 0) return data;
    const [source, target] = this.graph.extremities(edge);
    if (this.highlightKind) {
      const exempt = this.graph.getNodeAttribute(source, 'kind') === this.highlightKind
        || this.graph.getNodeAttribute(target, 'kind') === this.highlightKind;
      return exempt ? data : { ...data, color: edgeColorAt(this.fadeCurrent) };
    }
    if (source === this.hoveredNode || target === this.hoveredNode) return data;
    return { ...data, color: edgeColorAt(this.fadeCurrent) };
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

    // null for a node type discogs.com has no page for, or one missing its
    // own id -- hide the link rather than send someone to a broken URL.
    const link = $('panel-link');
    const url = discogsUrl(a.kind, a.discogsId);
    link.hidden = !url;
    if (url) link.href = url;
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
    $('panel-link').hidden = true;
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
