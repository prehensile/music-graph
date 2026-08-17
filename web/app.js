/*
 * Music Graph viewer.
 *
 * Talks only to this server's own JSON API -- no database credentials reach the
 * page and no Cypher is built here. Layout is Fruchterman-Reingold with a
 * cooling schedule (see runLayout), so hub-heavy Discogs subgraphs settle
 * instead of oscillating the way the original viewer's uncooled simulation did.
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

const $ = (id) => document.getElementById(id);

class Viewer {
  constructor() {
    this.graph = new graphology.Graph({ multi: false, type: 'undirected' });
    this.renderer = null;
    this.layout = null;
    this.selected = null;
    this.expanded = new Set();

    this.initRenderer();
    this.initLegend();
    this.bindEvents();
    this.loadStats();
  }

  // ---------------------------------------------------------------- setup

  initRenderer() {
    this.renderer = new Sigma(this.graph, $('graph'), {
      renderEdgeLabels: false,
      defaultNodeColor: COLOURS.Unknown,
      defaultEdgeColor: 'rgba(148,163,184,0.35)',
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
    });

    this.renderer.on('clickNode', ({ node }) => this.select(node));
    this.renderer.on('doubleClickNode', ({ node, event }) => {
      if (event && event.preventSigmaDefault) event.preventSigmaDefault();
      this.expand(node);
    });
    this.renderer.on('enterNode', () => { document.body.style.cursor = 'pointer'; });
    this.renderer.on('leaveNode', () => { document.body.style.cursor = 'default'; });
    this.renderer.on('clickStage', () => this.select(null));
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
      const term = $('q').value.trim();
      if (term) {
        $('q').blur();           // dismisses the on-screen keyboard
        this.search(term);
      }
    });
    $('reset').addEventListener('click', () => this.clear());
    $('panel-close').addEventListener('click', () => this.select(null));
    $('panel-expand').addEventListener('click', () => {
      if (this.selected) this.expand(this.selected);
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
    try {
      const stats = await this.api('/api/stats');
      const total = Object.values(stats).reduce((a, b) => a + b, 0);
      $('counts').textContent = total
        ? Object.entries(stats).map(([k, v]) => `${v.toLocaleString()} ${k}`).join(' · ')
        : 'empty graph';
    } catch (_) {
      $('counts').textContent = '';
    }
  }

  async search(term) {
    this.busy(true);
    try {
      const data = await this.api(`/api/search?q=${encodeURIComponent(term)}`);
      this.graph.clear();
      this.expanded.clear();
      this.select(null);
      const added = this.merge(data);
      $('hint').hidden = added > 0;
      if (!added) this.toast(`Nothing found for “${term}”`);
      this.runLayout();
    } catch (err) {
      this.toast(err.message);
    } finally {
      this.busy(false);
    }
  }

  async expand(nodeId) {
    if (this.expanded.has(nodeId)) return;
    this.expanded.add(nodeId);
    this.busy(true);
    try {
      const data = await this.api(`/api/expand?id=${encodeURIComponent(nodeId)}`);
      // Seed new nodes near their parent so the layout has a sane starting point.
      const origin = this.graph.hasNode(nodeId)
        ? this.graph.getNodeAttributes(nodeId)
        : { x: 0, y: 0 };
      const added = this.merge(data, origin);
      if (!added) this.toast('No further connections');
      this.runLayout();
    } catch (err) {
      this.expanded.delete(nodeId);
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
      if (this.graph.hasNode(n.id)) {
        // Keep the larger degree so a node does not shrink when re-seen in a
        // smaller subgraph.
        const prev = this.graph.getNodeAttribute(n.id, 'degree') || 0;
        if (n.degree > prev) {
          this.graph.setNodeAttribute(n.id, 'degree', n.degree);
          this.graph.setNodeAttribute(n.id, 'size', this.sizeFor(n.degree));
        }
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
      added += 1;
    });

    (data.edges || []).forEach((e) => {
      if (!this.graph.hasNode(e.source) || !this.graph.hasNode(e.target)) return;
      if (e.source === e.target) return;
      if (this.graph.hasEdge(e.source, e.target)) return;
      this.graph.addEdge(e.source, e.target, { type_: e.type, size: 1 });
    });

    return added;
  }

  sizeFor(degree) {
    return Math.max(4, Math.min(26, 4 + Math.log2((degree || 0) + 1) * 3.2));
  }

  /*
   * Fruchterman-Reingold with a cooling schedule, run in animation-frame
   * batches so the graph is seen to settle and the UI stays responsive.
   *
   * The view is capped at a few hundred nodes, so O(n^2) repulsion costs
   * nothing and Barnes-Hut is not worth a dependency. Cooling is the part the
   * original viewer lacked: it ran a constant-energy simulation forever, with
   * elastic collision impulses fighting the springs, so hub-heavy subgraphs
   * jittered indefinitely instead of coming to rest.
   */
  runLayout() {
    if (this.raf) cancelAnimationFrame(this.raf);
    const nodes = this.graph.nodes();
    if (!nodes.length) return;

    const k = Math.sqrt((820 * 820) / nodes.length); // ideal separation
    const MAX_ITER = 240;
    let temp = k * 0.85;
    let iter = 0;

    const step = () => {
      for (let pass = 0; pass < 4 && iter < MAX_ITER; pass += 1, iter += 1) {
        this.layoutTick(nodes, k, temp);
        temp *= 0.955;
      }
      this.renderer.refresh();
      if (iter < MAX_ITER) {
        this.raf = requestAnimationFrame(step);
      } else {
        this.raf = null;
        // Pull back slightly once settled: sigma fits node centres, which
        // leaves labels on the outermost nodes clipped at the viewport edge.
        this.renderer.getCamera().animate(
          { x: 0.5, y: 0.5, ratio: 1.2 }, { duration: 320 },
        );
      }
    };
    this.raf = requestAnimationFrame(step);
  }

  layoutTick(nodes, k, temp) {
    const g = this.graph;
    const dx = new Float64Array(nodes.length);
    const dy = new Float64Array(nodes.length);
    const index = new Map(nodes.map((n, i) => [n, i]));
    const pos = nodes.map((n) => g.getNodeAttributes(n));

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
        const f = (k * k) / d / d * k * 0.25;
        dx[i] += (ax / d) * f; dy[i] += (ay / d) * f;
        dx[j] -= (ax / d) * f; dy[j] -= (ay / d) * f;
      }
    }

    g.forEachEdge((_e, _a, source, target) => {
      const i = index.get(source);
      const j = index.get(target);
      if (i === undefined || j === undefined) return;
      const ax = pos[i].x - pos[j].x;
      const ay = pos[i].y - pos[j].y;
      const d = Math.hypot(ax, ay) || 0.01;
      const f = (d * d) / k;
      dx[i] -= (ax / d) * f; dy[i] -= (ay / d) * f;
      dx[j] += (ax / d) * f; dy[j] += (ay / d) * f;
    });

    for (let i = 0; i < nodes.length; i += 1) {
      const len = Math.hypot(dx[i], dy[i]) || 1;
      const capped = Math.min(len, temp);
      let x = pos[i].x + (dx[i] / len) * capped;
      let y = pos[i].y + (dy[i] / len) * capped;
      x -= x * 0.012;            // weak gravity keeps components in frame
      y -= y * 0.012;
      g.setNodeAttribute(nodes[i], 'x', x);
      g.setNodeAttribute(nodes[i], 'y', y);
    }
  }

  // ------------------------------------------------------------ selection

  select(nodeId) {
    this.selected = nodeId;
    const panel = $('panel');

    if (!nodeId || !this.graph.hasNode(nodeId)) {
      panel.hidden = true;
      this.graph.forEachNode((n) => this.graph.removeNodeAttribute(n, 'highlighted'));
      this.renderer.refresh();
      return;
    }

    const a = this.graph.getNodeAttributes(nodeId);
    $('panel-kind').textContent = a.kind;
    $('panel-kind').style.background = COLOURS[a.kind] || COLOURS.Unknown;
    $('panel-title').textContent = a.title;

    const neighbours = this.graph.neighbors(nodeId).length;
    const rows = [];
    if (a.realname) rows.push(['Real name', a.realname]);
    if (a.year) rows.push(['Year', a.year]);
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
    $('panel-expand').hidden = this.expanded.has(nodeId);
    panel.hidden = false;

    this.graph.forEachNode((n) => this.graph.setNodeAttribute(n, 'highlighted', n === nodeId));
    this.renderer.refresh();
  }

  clear() {
    this.graph.clear();
    this.expanded.clear();
    this.select(null);
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
