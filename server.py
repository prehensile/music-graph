#!/usr/bin/env python3
"""
Read-only HTTP front end for the music graph.

Serves the static viewer plus a narrow JSON API. Every Cypher statement lives
here, is parameterised, and only reads -- the browser never receives database
credentials and cannot submit queries of its own. That is the main structural
difference from the original viewer, which opened a bolt connection straight
from the page with the password inlined in a JS file, and interpolated user
input into Cypher strings.

Environment:
    NEO4J_URI       default bolt://localhost:7687
    NEO4J_USERNAME  default neo4j
    NEO4J_PASSWORD  required
    NEO4J_DATABASE  default neo4j
    APP_USER        basic-auth username, default 'music'
    APP_PASSWORD    basic-auth password, required unless APP_ALLOW_ANONYMOUS=1
    APP_PORT        default 80
    APP_BIND        default 0.0.0.0
"""

import base64
import hmac
import json
import os
import re
import sys
import threading
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from neo4j import GraphDatabase, basic_auth


WEB_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
SEARCH_INDEX = "entitySearch"
MAX_LIMIT = 300

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USERNAME = os.environ.get("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.environ.get("NEO4J_DATABASE", "neo4j")

APP_USER = os.environ.get("APP_USER", "music")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
APP_ALLOW_ANONYMOUS = os.environ.get("APP_ALLOW_ANONYMOUS") == "1"

# Written by provision_droplet.sh. Read-only here.
LOG_FILE = os.environ.get("LOG_FILE", "/var/log/music-graph.log")
STATUS_FILE = os.environ.get("STATUS_FILE", "/var/log/music-graph.status")
STEPS_FILE = os.environ.get("STEPS_FILE", "/var/log/music-graph.steps.jsonl")
LOG_TAIL_BYTES = 64 * 1024
LOG_TAIL_LINES = 200

NODE_LABELS = ("Artist", "Group", "Release", "Label")
# The four relationship types in the Data Model (see CLAUDE.md), ordered
# rarest/structural first: a Group's few MEMBER_OF edges or a Label's few
# SUBLABEL edges should never lose out to a node's own CREDITED or
# RELEASED_ON edges, which run into the hundreds for a hub. _neighbours
# spends a node's LIMIT budget through this list in order, so the front
# always gets filled first and the back only gets whatever's left.
REL_TYPES = ("MEMBER_OF", "SUBLABEL", "RELEASED_ON", "CREDITED")

# Lucene reserved characters. The full-text index is the only place user text
# reaches the database as anything other than a bound parameter, so the term is
# scrubbed rather than trusted.
_LUCENE_SPECIAL = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')


def clean_term(raw):
    """Reduce free text to something safe to hand the full-text index."""
    term = _LUCENE_SPECIAL.sub(" ", raw or "").strip()
    return re.sub(r"\s+", " ", term)[:120]


def clamp_limit(raw, default=60):
    try:
        return max(1, min(MAX_LIMIT, int(raw)))
    except (TypeError, ValueError):
        return default


# Separate, smaller cap from MAX_LIMIT: a live-typing dropdown wants a
# generous handful of top matches, not the couple hundred nodes a committed
# search expands to.
SUGGEST_LIMIT_MAX = 20


def clamp_suggest_limit(raw, default=10):
    try:
        return max(1, min(SUGGEST_LIMIT_MAX, int(raw)))
    except (TypeError, ValueError):
        return default


# _rank_matches (see below) always fetches this many candidates from the
# full-text index, regardless of how few it will actually return -- fixed
# rather than scaled by the caller's `limit`, so a match sitting well down
# the plain full-text ranking still has a fair pool to be promoted from by
# the neighbour-term boost. A real query has shown a correct match sitting
# ~40 places down the raw full-text order (see
# notes/search-relevance-2026-08-19.md), which is why these aren't small.
SEARCH_POOL_MAX = 60
SUGGEST_POOL_MAX = 60

# Added to a candidate's Lucene score per query term that its own name/title
# is missing but a directly-connected artist or label carries instead.
# Measured Lucene scores here run roughly 5-8 for a short exact-text match
# (see notes/search-relevance-2026-08-19.md), so this is sized to outweigh
# that gap -- a genuine cross-relationship match should win outright, not
# nudge ahead by a fraction.
NEIGHBOUR_TERM_BOOST = 6.0


def tail_lines(path, max_bytes=LOG_TAIL_BYTES, max_lines=LOG_TAIL_LINES):
    """Last lines of a file, without reading the whole thing into memory."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()          # discard the partial first line
            data = fh.read()
    except OSError:
        return []
    text = data.decode("utf-8", "replace")
    return [ln for ln in text.splitlines() if ln.strip()][-max_lines:]


def read_progress():
    """
    Assemble the provisioning state from the files the shell script writes.

    Deliberately touches no database: this is the one view that has to work
    while the import is still running, and before Neo4j has anything in it.
    """
    steps, seen = [], {}
    try:
        with open(STEPS_FILE, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                # Later records for a step supersede earlier ones, so a step
                # goes running -> done/failed in place.
                seen[rec.get("n")] = rec
    except OSError:
        pass
    steps = [seen[k] for k in sorted(seen)]

    status = ""
    try:
        with open(STATUS_FILE, "r", encoding="utf-8", errors="replace") as fh:
            status = fh.read().strip()
    except OSError:
        pass

    total = steps[0].get("of") if steps else 0
    # A skipped step (START_AT) is not outstanding work, so count it as settled
    # or the page would sit at "in progress" forever after a partial re-run.
    done = sum(1 for s in steps if s.get("state") in ("done", "skipped"))
    failed = any(s.get("state") == "failed" for s in steps)
    running = next((s for s in steps if s.get("state") == "running"), None)

    return {
        "status": status,
        "steps": steps,
        "total": total,
        "done": done,
        "failed": failed,
        "running": running,
        "finished": bool(total) and done >= total and not failed,
        "log": tail_lines(LOG_FILE),
    }


class Graph:
    """Thin wrapper over the driver holding the queries the API exposes."""

    def __init__(self):
        if not NEO4J_PASSWORD:
            sys.exit("NEO4J_PASSWORD is not set")
        self._driver = GraphDatabase.driver(
            NEO4J_URI, auth=basic_auth(NEO4J_USERNAME, NEO4J_PASSWORD)
        )
        self._stats_cache = None
        self._lock = threading.Lock()

    def close(self):
        self._driver.close()

    def _run(self, cypher, **params):
        with self._driver.session(database=NEO4J_DATABASE) as session:
            return list(session.run(cypher, **params))

    def stats(self):
        """Per-label totals. Counting a single label hits the count store, so
        each of these is O(1) rather than a scan of ~15M nodes."""
        with self._lock:
            if self._stats_cache is not None:
                return self._stats_cache
            counts = {}
            for label in NODE_LABELS:
                rows = self._run(f"MATCH (n:{label}) RETURN count(n) AS c")
                counts[label] = rows[0]["c"] if rows else 0
            # Only cache a populated result. The service starts before the
            # import, so an all-zero answer here is "not loaded yet", not a
            # fact worth remembering for the life of the process.
            if any(counts.values()):
                self._stats_cache = counts
            return counts

    def search(self, term, limit):
        term = clean_term(term)
        if not term:
            return {"nodes": [], "edges": []}
        seeds = max(1, limit // 4)
        matches = self._rank_matches(term, seeds, SEARCH_POOL_MAX)
        return self._expand_two_hop([m["id"] for m in matches], limit)

    def suggest(self, term, limit):
        """
        Candidates for the live-search dropdown: just the top full-text
        matches with enough descriptive text to tell them apart before
        committing to a search -- an artist or group's own bio, a label's
        bio (it already has one, same property), and for a release, the
        artist(s), label(s) and year credited on it since a release has no
        bio of its own. Deliberately not `_expand_two_hop` -- that's the
        weight of a committed search, not every keystroke.
        """
        term = clean_term(term)
        if not term:
            return {"results": []}
        matches = self._rank_matches(term, limit, SUGGEST_POOL_MAX)
        # `score` only exists to rank matches against each other; the client
        # has no use for it.
        return {"results": [{k: v for k, v in m.items() if k != "score"} for m in matches]}

    def _rank_matches(self, term, limit, pool):
        """
        Full-text matches for `term`, re-scored by a query-time approximation
        of denormalizing credited-artist/label text onto the node -- see
        notes/search-relevance-2026-08-19.md (Option B there). `entitySearch`
        indexes each node's own name/title in isolation, so a Release titled
        just "Timeless" can never outscore an unrelated release literally
        titled "Goldie" for the query "goldie timeless", even though the
        first is exactly what searching both words together means: the word
        "Goldie" lives on the *artist* node, one CREDITED hop away, and
        Lucene has no way to see across it.

        Fixing that properly (Option A in the note) means denormalizing that
        text onto Release itself and widening the index -- a live reindex
        over millions of nodes, or a pipeline change plus a full rebuild.
        This is the cheap stand-in: fetch a wider pool of candidates than
        `limit` asks for, and for each one still missing a query term in its
        own text, check whether a directly CREDITED artist or RELEASED_ON
        label carries it instead -- the same neighbour data `suggest` already
        wants for its descriptive text, so this costs one extra hop per
        candidate rather than a second round trip.
        """
        terms = [t for t in term.lower().split() if t]
        rows = self._run(
            """
            CALL db.index.fulltext.queryNodes($index, $term, {limit: $pool})
            YIELD node, score
            OPTIONAL MATCH (node)<-[:CREDITED]-(artist)
            OPTIONAL MATCH (node)-[:RELEASED_ON]->(label)
            WITH node, score,
                 collect(DISTINCT artist.name)[0..3] AS artists,
                 collect(DISTINCT label.name)[0..2] AS labelNames
            RETURN elementId(node) AS id, labels(node) AS nodeLabels,
                   coalesce(node.name, node.title) AS title,
                   node.year AS year,
                   left(coalesce(node.profile, ''), 200) AS profile,
                   score, artists, labelNames
            """,
            index=SEARCH_INDEX, term=term, pool=pool,
        )
        matches = []
        for r in rows:
            kind = next((l for l in (r["nodeLabels"] or []) if l in NODE_LABELS), "Unknown")
            title = r["title"] or "(untitled)"
            artists = [a for a in (r["artists"] or []) if a]
            labels = [l for l in (r["labelNames"] or []) if l]
            own_text = title.lower()
            neighbour_names = [n.lower() for n in artists + labels]
            boost = sum(
                NEIGHBOUR_TERM_BOOST for t in terms
                if t not in own_text and any(t in n for n in neighbour_names)
            )
            matches.append({
                "id": r["id"],
                "kind": kind,
                "title": title,
                "year": r["year"] or "",
                "profile": r["profile"] or "",
                "artists": artists,
                "labels": labels,
                "score": (r["score"] or 0.0) + boost,
            })
        matches.sort(key=lambda m: m["score"], reverse=True)
        return matches[:limit]

    def expand(self, element_id, limit):
        return self._expand_two_hop([element_id], limit)

    def _expand_two_hop(self, ids, limit):
        """
        First- and second-order neighbours of `ids`: whatever a user selects or
        searches for should arrive with enough surrounding graph to explore
        without a second round trip per tap. Two separate LIMIT-ed queries
        rather than one two-hop Cypher pattern, since a hop straight through a
        hub node (e.g. "Various") would otherwise fan out combinatorially
        before LIMIT ever got a chance to apply.
        """
        if not ids:
            return {"nodes": [], "edges": []}
        hop1 = self._neighbours(ids, limit)
        hop2 = self._neighbours([n["id"] for n in hop1["nodes"]], limit)
        merged = self._merge_payloads(hop1, hop2)
        return self._cap_payload(merged, MAX_LIMIT)

    def _neighbours(self, ids, limit):
        """
        A flat `LIMIT $limit` over `(n)-[r]-(m)` gives every relationship type
        one shared budget, in whatever order Neo4j happens to enumerate them
        -- for a prolific node that meant its hundreds of CREDITED edges
        exhausted the limit before its handful of MEMBER_OF edges were ever
        reached, so e.g. tapping a band's own node expanded it without ever
        showing its members.

        Fetched as a chain of per-type batches instead, in REL_TYPES'
        rarest-first order, each spending only what's left of $limit after
        the ones before it: MEMBER_OF and SUBLABEL fill up first and always
        get in, CREDITED and RELEASED_ON only get whatever budget survives
        them. A node's total is still exactly $limit -- unlike giving every
        type its own full $limit, which would need `_cap_payload` to
        routinely mop up the overshoot instead of only for its documented
        role as a rare backstop.
        """
        stages = []
        carried = []          # batch{i} names accumulated so far, passed through each WITH
        remaining_expr = "$limit"
        for i, rel_type in enumerate(REL_TYPES):
            r, m, coll, batch, left = f"r{i}", f"m{i}", f"c{i}", f"batch{i}", f"left{i}"
            pass_through = "".join(f"{name}, " for name in carried)
            # OPTIONAL, not MATCH: a node with none of this type must still
            # flow through to the remaining stages rather than dropping out.
            stages.append(f"""
                OPTIONAL MATCH (n)-[{r}:{rel_type}]-({m})
                WITH n, {pass_through}{remaining_expr} AS budget{i},
                     [x IN collect({{r: {r}, m: {m}}}) WHERE x.r IS NOT NULL] AS {coll}
                WITH n, {pass_through}{coll}[0..budget{i}] AS {batch},
                     budget{i} - size({coll}[0..budget{i}]) AS {left}
            """)
            carried.append(batch)
            remaining_expr = left
        rows_expr = " + ".join(carried)

        # Relationship type names come from the fixed REL_TYPES constant,
        # never from user input, so interpolating them into the query text
        # is safe.
        rows = self._run(
            f"""
            UNWIND $ids AS id
            MATCH (n) WHERE elementId(n) = id
            CALL {{
                WITH n
                {"".join(stages)}
                WITH n, {rows_expr} AS rows
                UNWIND (CASE WHEN size(rows) = 0 THEN [{{r: null, m: null}}] ELSE rows END) AS row
                RETURN row.r AS r, row.m AS m
            }}
            RETURN n, r, m
            """,
            ids=ids, limit=limit,
        )
        return self._payload(rows)

    @staticmethod
    def _merge_payloads(*payloads):
        nodes, edges = {}, {}
        for payload in payloads:
            for n in payload["nodes"]:
                nodes.setdefault(n["id"], n)
            for e in payload["edges"]:
                edges.setdefault(e["id"], e)
        # Degree within the merged (two-hop) set, not either hop on its own.
        for n in nodes.values():
            n["degree"] = 0
        for e in edges.values():
            for end in ("source", "target"):
                if e[end] in nodes:
                    nodes[e[end]]["degree"] += 1
        return {"nodes": list(nodes.values()), "edges": list(edges.values())}

    @staticmethod
    def _cap_payload(payload, cap):
        """
        Safety net, not the normal path: two independently-LIMIT-ed hops can
        together exceed `cap` even though each stayed within `limit`. The
        client's O(n^2) layout is built for a few hundred nodes, so trim
        rather than hand it more.
        """
        if len(payload["nodes"]) <= cap:
            return payload
        keep = {n["id"] for n in payload["nodes"][:cap]}
        return {
            "nodes": [n for n in payload["nodes"] if n["id"] in keep],
            "edges": [e for e in payload["edges"]
                      if e["source"] in keep and e["target"] in keep],
        }

    def _payload(self, rows):
        """Flatten (n, r, m) rows into a node/edge payload for the client."""
        nodes, edges = {}, {}

        def add_node(node):
            if node is None:
                return
            eid = node.element_id
            if eid in nodes:
                return
            labels = list(node.labels)
            props = dict(node)
            nodes[eid] = {
                "id": eid,
                "label": labels[0] if labels else "Unknown",
                "title": props.get("name") or props.get("title") or "(untitled)",
                "year": props.get("year") or "",
                "realname": props.get("realname") or "",
                # Profiles run to thousands of words; the panel only previews.
                "profile": (props.get("profile") or "")[:600],
                "degree": 0,
            }

        for row in rows:
            n, r, m = row.get("n"), row.get("r"), row.get("m")
            add_node(n)
            add_node(m)
            if r is not None:
                eid = r.element_id
                if eid not in edges:
                    edges[eid] = {
                        "id": eid,
                        "source": r.start_node.element_id,
                        "target": r.end_node.element_id,
                        "type": r.type,
                    }

        # Degree within the returned subgraph drives node size. Using the true
        # global degree would make hub nodes swamp the view.
        for edge in edges.values():
            for end in ("source", "target"):
                if edge[end] in nodes:
                    nodes[edge[end]]["degree"] += 1

        return {"nodes": list(nodes.values()), "edges": list(edges.values())}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, graph=None, **kwargs):
        self.graph = graph
        super().__init__(*args, directory=WEB_ROOT, **kwargs)

    # Quieter logs: one line per request, no HTML error pages.
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _authorised(self):
        if APP_ALLOW_ANONYMOUS:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8", "replace")
            user, _, password = decoded.partition(":")
        except Exception:
            return False
        return (hmac.compare_digest(user, APP_USER)
                and hmac.compare_digest(password, APP_PASSWORD))

    def _send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authorised():
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="discograph"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        parsed = urlparse(self.path)

        # /logs is a page, not a directory; map it onto the static file.
        if parsed.path in ("/logs", "/logs/"):
            self.path = "/logs.html"
            return super().do_GET()

        if not parsed.path.startswith("/api/"):
            return super().do_GET()

        # Served before anything else, and without touching the database: this
        # is what you watch while the import is still running.
        if parsed.path == "/api/progress":
            return self._send_json(read_progress())

        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/stats":
                # An unreachable or empty database is the normal state until
                # the import lands, not a server error. Answer 200 with empty
                # counts and let the page point the reader at /logs.
                try:
                    self._send_json(self.graph.stats())
                except Exception as exc:
                    sys.stderr.write(f"stats unavailable: {exc!r}\n")
                    self._send_json({})
            elif parsed.path == "/api/search":
                self._send_json(self.graph.search(
                    query.get("q", [""])[0],
                    clamp_limit(query.get("limit", [None])[0]),
                ))
            elif parsed.path == "/api/suggest":
                self._send_json(self.graph.suggest(
                    query.get("q", [""])[0],
                    clamp_suggest_limit(query.get("limit", [None])[0]),
                ))
            elif parsed.path == "/api/expand":
                node_id = query.get("id", [""])[0]
                if not node_id:
                    self._send_json({"error": "id required"}, HTTPStatus.BAD_REQUEST)
                else:
                    self._send_json(self.graph.expand(
                        node_id, clamp_limit(query.get("limit", [None])[0]),
                    ))
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            # Log the detail, return something generic.
            sys.stderr.write(f"query failed: {exc!r}\n")
            self._send_json({"error": "query failed"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main():
    if not APP_ALLOW_ANONYMOUS and not APP_PASSWORD:
        sys.exit("APP_PASSWORD is not set (or set APP_ALLOW_ANONYMOUS=1 to disable auth)")

    graph = Graph()
    port = int(os.environ.get("APP_PORT", "80"))
    bind = os.environ.get("APP_BIND", "0.0.0.0")

    server = ThreadingHTTPServer((bind, port), partial(Handler, graph=graph))
    print(f"serving {WEB_ROOT} on http://{bind}:{port} -> {NEO4J_URI}/{NEO4J_DATABASE}",
          file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        graph.close()


if __name__ == "__main__":
    main()
