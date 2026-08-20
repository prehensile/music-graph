#!/usr/bin/env python3
"""
Export a flat (type, id, name) CSV straight from Neo4j, for building a static
search index once the intermediate CSVs are gone.

Filed alongside notes/static-search-downsize-2026-08-19.md: the plan there
needs id+name for every node, and the pipeline's own artists.csv/groups.csv/
releases.csv/labels.csv won't exist any more after the downsize. This reads
the live graph instead, which is available for as long as Neo4j is, and is
strictly better as a source anyway -- it reflects the graph as actually
imported (post dedup, post the placeholder-artist filtering, etc.) rather
than the pre-import CSVs.

`id` is each node's original Discogs id (n.artistId / n.groupId / n.releaseId
/ n.labelId), not Neo4j's elementId -- elementId is specific to one Neo4j
store and will not survive a dump/load migration or a future reimport, which
defeats the point of exporting something meant to outlive this Neo4j
instance. See CLAUDE.md's Data Model section for why Artist and Group share
one id space (Entity) while Release and Label are each their own: `id` alone
is therefore only unique *within* a `type` -- a Release and a Label can
legitimately share a numeric id -- so every row carries `type` and downstream
consumers must key on the (type, id) pair, never id alone.

    NEO4J_PASSWORD=... python export_search_index.py
    NEO4J_PASSWORD=... python export_search_index.py -o search_export.csv.gz
    NEO4J_PASSWORD=... python export_search_index.py --labels Artist,Group

Reads straight from the Bolt result stream rather than materialising it, so
memory stays flat regardless of node count. Rows with an empty or missing
name are skipped -- they can never be searched for, and CLAUDE.md notes a
small amount of referential drift in the graph, so a handful showing up here
is expected, not a bug.

Environment matches server.py/graph_stats.py: NEO4J_URI, NEO4J_USERNAME,
NEO4J_PASSWORD, NEO4J_DATABASE.
"""

import argparse
import csv
import gzip
import os
import sys

from neo4j import GraphDatabase, basic_auth

from progress import Heartbeat

# label -> (id property, name property)
LABEL_PROPS = {
    "Artist": ("artistId", "name"),
    "Group": ("groupId", "name"),
    "Release": ("releaseId", "title"),
    "Label": ("labelId", "name"),
}


def connect():
    password = os.environ.get("NEO4J_PASSWORD", "")
    if not password:
        sys.exit("NEO4J_PASSWORD is not set")
    return GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=basic_auth(os.environ.get("NEO4J_USERNAME", "neo4j"), password),
    )


def count(session, label):
    """O(1) via the count store -- just for the Heartbeat's ETA, not a scan."""
    return session.run(f"MATCH (n:{label}) RETURN count(n) AS c").single()["c"]


def export_label(session, label, writer):
    id_prop, name_prop = LABEL_PROPS[label]
    hb = Heartbeat(label.lower(), total=count(session, label), unit=label.lower())
    hb.begin()
    written = skipped = 0
    result = session.run(f"MATCH (n:{label}) RETURN n.{id_prop} AS id, n.{name_prop} AS name")
    for record in result:  # streamed from the Bolt connection, not buffered
        node_id, name = record["id"], record["name"]
        if not name or not name.strip():
            skipped += 1
            hb.tick(written + skipped)
            continue
        writer.writerow((label, node_id, name))
        written += 1
        hb.tick(written + skipped)
    hb.finish(f"{skipped:,} skipped (empty name)")
    return written, skipped


def open_output(path):
    if path.endswith(".gz"):
        return gzip.open(path, "wt", newline="", encoding="utf-8")
    return open(path, "w", newline="", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("-o", "--output", default="search_index_export.csv",
                     help="output path; .gz extension writes gzip-compressed "
                          "(default: %(default)s)")
    ap.add_argument("--labels", default=",".join(LABEL_PROPS),
                     help=f"comma-separated subset of {list(LABEL_PROPS)} "
                          "(default: all)")
    args = ap.parse_args()

    labels = [label.strip() for label in args.labels.split(",") if label.strip()]
    unknown = [label for label in labels if label not in LABEL_PROPS]
    if unknown:
        sys.exit(f"unknown label(s): {', '.join(unknown)} -- choose from {list(LABEL_PROPS)}")

    driver = connect()
    database = os.environ.get("NEO4J_DATABASE", "neo4j")
    total_written = total_skipped = 0
    try:
        with driver.session(database=database) as session, open_output(args.output) as fh:
            writer = csv.writer(fh)
            writer.writerow(("type", "id", "name"))
            for label in labels:
                written, skipped = export_label(session, label, writer)
                total_written += written
                total_skipped += skipped
    finally:
        driver.close()

    print(f"\n{total_written:,} rows written to {args.output}"
          f" ({total_skipped:,} skipped for empty name)", file=sys.stderr)


if __name__ == "__main__":
    main()
