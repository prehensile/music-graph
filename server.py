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
    done = sum(1 for s in steps if s.get("state") == "done")
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
        # Seeds come from the full-text index; a CONTAINS scan over millions of
        # nodes would not return in reasonable time.
        rows = self._run(
            f"""
            CALL db.index.fulltext.queryNodes($index, $term, {{limit: $seeds}})
            YIELD node AS n
            OPTIONAL MATCH (n)-[r]-(m)
            RETURN n, r, m
            LIMIT $limit
            """,
            index=SEARCH_INDEX, term=term,
            seeds=max(1, limit // 4), limit=limit,
        )
        return self._payload(rows)

    def expand(self, element_id, limit):
        rows = self._run(
            """
            MATCH (n) WHERE elementId(n) = $id
            OPTIONAL MATCH (n)-[r]-(m)
            RETURN n, r, m
            LIMIT $limit
            """,
            id=element_id, limit=limit,
        )
        return self._payload(rows)

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
            self.send_header("WWW-Authenticate", 'Basic realm="music-graph"')
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
