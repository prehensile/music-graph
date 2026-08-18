#!/usr/bin/env python3
"""
Report on a built music graph: what it contains, what it cost to build, and
whether it looks sane.

Run it after provisioning and again after any later build, so successive runs
can be compared -- dump sizes and record counts drift month to month, and the
step timings are the only honest basis for estimating the next run.

Everything in the default report is cheap. Per-label and per-type counts come
from Neo4j's count store, which answers them without touching the data, so the
whole summary returns in about a second on a 15M-node graph. The queries that
genuinely scan live behind --deep and are individually fault-tolerant, so one
slow or failing query does not cost you the rest of the report.

    python graph_stats.py                     # fast summary
    python graph_stats.py --deep              # plus hubs, orphans, year spread
    python graph_stats.py --json -o stats.json

Environment matches server.py: NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD,
NEO4J_DATABASE. DATA_DIR, STEPS_FILE and NEO4J_DATA_DIR default to the
locations provision_droplet.sh uses.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

from neo4j import GraphDatabase, basic_auth

NODE_LABELS = ("Artist", "Group", "Release", "Label")
REL_TYPES = ("MEMBER_OF", "CREDITED", "RELEASED_ON", "SUBLABEL")

DATA_DIR = os.environ.get("DATA_DIR", "/var/lib/music-graph/data")
STEPS_FILE = os.environ.get("STEPS_FILE", "/var/log/music-graph.steps.jsonl")
NEO4J_DATA_DIR = os.environ.get("NEO4J_DATA_DIR", "/var/lib/neo4j/data")


# ------------------------------------------------------------------ helpers

def human_bytes(n):
    if not n:
        return "-"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def human_seconds(s):
    if s is None:
        return "-"
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


# --------------------------------------------------------------- provenance

def read_steps(path):
    """Per-step timings from the provisioning log, latest record per step."""
    seen = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                seen[rec.get("n")] = rec
    except OSError:
        return []
    return [seen[k] for k in sorted(k for k in seen if k is not None)]


def read_artifacts(data_dir):
    """Sizes of the dumps and the generated CSVs."""
    dumps, csvs, dump_date = [], [], None
    try:
        for name in sorted(os.listdir(data_dir)):
            full = os.path.join(data_dir, name)
            if not os.path.isfile(full):
                continue
            if name.endswith(".xml.gz"):
                dumps.append((name, os.path.getsize(full)))
                # discogs_20260801_artists.xml.gz -> 20260801
                parts = name.split("_")
                if len(parts) > 1 and parts[1].isdigit():
                    dump_date = parts[1]
    except OSError:
        pass

    csv_dir = os.path.join(data_dir, "csv")
    try:
        for name in sorted(os.listdir(csv_dir)):
            if name.endswith(".csv"):
                csvs.append((name, os.path.getsize(os.path.join(csv_dir, name))))
    except OSError:
        pass
    return dump_date, dumps, csvs


# -------------------------------------------------------------------- graph

class Stats:
    def __init__(self):
        password = os.environ.get("NEO4J_PASSWORD", "")
        if not password:
            sys.exit("NEO4J_PASSWORD is not set")
        self.database = os.environ.get("NEO4J_DATABASE", "neo4j")
        self.driver = GraphDatabase.driver(
            os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
            auth=basic_auth(os.environ.get("NEO4J_USERNAME", "neo4j"), password),
        )

    def close(self):
        self.driver.close()

    def run(self, cypher, **params):
        with self.driver.session(database=self.database) as session:
            return list(session.run(cypher, **params))

    def try_run(self, description, cypher, **params):
        """Deep queries scan; let one failure or timeout cost only itself."""
        started = time.time()
        try:
            rows = self.run(cypher, **params)
            return rows, time.time() - started, None
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            return [], time.time() - started, f"{type(exc).__name__}: {exc}"

    # -- cheap: answered from the count store, no scan ----------------------

    def node_counts(self):
        return {label: self.run(f"MATCH (n:{label}) RETURN count(n) AS c")[0]["c"]
                for label in NODE_LABELS}

    def rel_counts(self):
        return {rel: self.run(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS c")[0]["c"]
                for rel in REL_TYPES}

    # -- deep: these scan ---------------------------------------------------

    def hubs(self, label, limit=10):
        return self.try_run(
            f"top {label} by degree",
            f"""MATCH (n:{label})
                RETURN coalesce(n.name, n.title) AS name,
                       COUNT {{ (n)--() }} AS degree
                ORDER BY degree DESC LIMIT $limit""",
            limit=limit,
        )

    def orphans(self, label):
        return self.try_run(
            f"unconnected {label}",
            f"MATCH (n:{label}) WHERE NOT (n)--() RETURN count(n) AS c",
        )

    def release_decades(self):
        return self.try_run(
            "releases by decade",
            """MATCH (r:Release)
               WHERE r.year IS NOT NULL AND size(r.year) >= 4
               WITH left(r.year, 3) + '0s' AS decade
               RETURN decade, count(*) AS c ORDER BY decade""",
        )


# ------------------------------------------------------------------ collect

def collect(deep):
    dump_date, dumps, csvs = read_artifacts(DATA_DIR)
    report = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ"),
        "dump_date": dump_date,
        "steps": read_steps(STEPS_FILE),
        "dumps": dumps,
        "csvs": csvs,
        "neo4j_store_bytes": dir_size(NEO4J_DATA_DIR),
        "deep": {},
    }

    stats = Stats()
    try:
        report["nodes"] = stats.node_counts()
        report["relationships"] = stats.rel_counts()

        if deep:
            deep_out = {"hubs": {}, "orphans": {}, "timings": {}, "errors": {}}
            for label in NODE_LABELS:
                rows, secs, err = stats.hubs(label)
                deep_out["hubs"][label] = [(r["name"], r["degree"]) for r in rows]
                deep_out["timings"][f"hubs:{label}"] = secs
                if err:
                    deep_out["errors"][f"hubs:{label}"] = err
            for label in NODE_LABELS:
                rows, secs, err = stats.orphans(label)
                deep_out["orphans"][label] = rows[0]["c"] if rows else None
                deep_out["timings"][f"orphans:{label}"] = secs
                if err:
                    deep_out["errors"][f"orphans:{label}"] = err
            rows, secs, err = stats.release_decades()
            deep_out["decades"] = [(r["decade"], r["c"]) for r in rows]
            deep_out["timings"]["decades"] = secs
            if err:
                deep_out["errors"]["decades"] = err
            report["deep"] = deep_out
    finally:
        stats.close()
    return report


# ------------------------------------------------------------------- render

def render(report):
    out = []
    add = out.append
    nodes = report.get("nodes", {})
    rels = report.get("relationships", {})
    total_nodes = sum(nodes.values())
    total_rels = sum(rels.values())

    add("=" * 62)
    add(f"  music-graph statistics   {report['generated']}")
    if report.get("dump_date"):
        add(f"  Discogs dump             {report['dump_date']}")
    add("=" * 62)

    add("\nNODES")
    for label, count in sorted(nodes.items(), key=lambda kv: -kv[1]):
        share = count / total_nodes * 100 if total_nodes else 0
        add(f"  {label:<10} {count:>12,}   {share:5.1f}%")
    add(f"  {'total':<10} {total_nodes:>12,}")

    add("\nRELATIONSHIPS")
    for rel, count in sorted(rels.items(), key=lambda kv: -kv[1]):
        share = count / total_rels * 100 if total_rels else 0
        add(f"  {rel:<12} {count:>12,}   {share:5.1f}%")
    add(f"  {'total':<12} {total_rels:>12,}")
    if total_nodes:
        add(f"\n  {total_rels / total_nodes:.2f} relationships per node")
    if nodes.get("Release"):
        add(f"  {rels.get('CREDITED', 0) / nodes['Release']:.2f} credited artists per release")
    if nodes.get("Group"):
        add(f"  {rels.get('MEMBER_OF', 0) / nodes['Group']:.2f} members per group")

    steps = report.get("steps") or []
    if steps:
        add("\nBUILD")
        total = 0
        for step in steps:
            state = step.get("state", "?")
            elapsed = step.get("elapsed") or 0
            if state == "done":
                total += elapsed
            mark = {"done": "ok", "failed": "FAILED", "running": "..."}.get(state, state)
            add(f"  {step.get('n', '?'):>2}. {step.get('name', '?'):<34}"
                f"{human_seconds(elapsed):>10}  {mark}")
        add(f"  {'':>4}{'total':<34}{human_seconds(total):>10}")

    dumps, csvs = report.get("dumps") or [], report.get("csvs") or []
    if dumps or csvs:
        add("\nON DISK")
        if dumps:
            add(f"  dumps           {human_bytes(sum(s for _, s in dumps)):>10}"
                f"   ({len(dumps)} files)")
            for name, size in dumps:
                add(f"    {name:<44}{human_bytes(size):>10}")
        if csvs:
            add(f"  import CSVs     {human_bytes(sum(s for _, s in csvs)):>10}"
                f"   ({len(csvs)} files)")
            for name, size in csvs:
                add(f"    {name:<44}{human_bytes(size):>10}")
        if report.get("neo4j_store_bytes"):
            add(f"  Neo4j store     {human_bytes(report['neo4j_store_bytes']):>10}")

    deep = report.get("deep") or {}
    if deep:
        hub_sets = {k: v for k, v in (deep.get("hubs") or {}).items() if v}
        if hub_sets:
            add("\nHUBS  (highest degree, within the whole graph)")
        for label, rows in hub_sets.items():
            add(f"  {label}")
            for name, degree in rows[:5]:
                shown = (name or "(untitled)")[:44]
                add(f"    {shown:<46}{degree:>10,}")

        orphans = deep.get("orphans") or {}
        if any(v is not None for v in orphans.values()):
            add("\nUNCONNECTED NODES")
            for label, count in orphans.items():
                if count is None:
                    continue
                total_label = nodes.get(label, 0)
                share = count / total_label * 100 if total_label else 0
                add(f"  {label:<10} {count:>12,}   {share:5.1f}% of {label}")

        decades = deep.get("decades") or []
        if decades:
            add("\nRELEASES BY DECADE")
            peak = max(c for _, c in decades) or 1
            for decade, count in decades:
                bar = "#" * int(count / peak * 32)
                add(f"  {decade:<8}{count:>10,}  {bar}")

        errors = deep.get("errors") or {}
        for what, err in errors.items():
            add(f"\n  ! {what} failed: {err}")

    add("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="Summarise a built music graph.")
    ap.add_argument("--deep", action="store_true",
                    help="include queries that scan (hubs, orphans, decades)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    ap.add_argument("-o", "--output", help="write to a file as well as stdout")
    args = ap.parse_args()

    report = collect(deep=args.deep)
    text = json.dumps(report, indent=2, default=str) if args.json else render(report)
    print(text)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
