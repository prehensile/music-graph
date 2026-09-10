#!/usr/bin/env python3
"""
Delete "Not On Label" placeholder Label nodes -- and every edge touching
them -- from an already-imported, live graph.

discogs_to_neo4j.py now refuses to create these going forward (see
is_placeholder_label / the RELEASED_ON gate in process_release), but that
only affects a future re-import from scratch. This is the live-migration
counterpart, in the spirit of migrate_add_aliases.py: patch the graph that
is already sitting there rather than reimport hours of data to pick up one
data-quality fix. Measured on the 2026-08 dump: 313,957 of 2,420,829 Label
nodes (13%) match -- the canonical "Not On Label" (id 1818) plus one
freshly-minted "Not On Label (Artist Self-released)" id per self-released
item, none of them a real record label.

Imports is_placeholder_label from discogs_to_neo4j so this and the
transform can't drift apart on what counts as a placeholder -- same reason
migrate_add_aliases.py imports iter_artist_aliases rather than
reimplementing the extraction.

Unlike migrate_add_aliases.py's MERGE (idempotent, safe to run by default),
this deletes. Re-running it is still safe -- a node already gone just
doesn't match a second time -- but the risk profile of a bad run is not
symmetric with an additive migration, so this defaults to a dry run
(counts + a name sample, nothing touched) and needs --execute to actually
delete.

No XML dump is read: this operates purely on what's already in the graph,
matching Label nodes by their *name* property directly rather than
resolving an external id to a node -- the same reason it needs no index
(labelId is not indexed after a bulk import, see CLAUDE.md's Known
Issues), since matching and deleting go by elementId(n), which is always
resolvable in O(1), same trick server.py uses throughout.

Reads and writes go through two separate driver sessions, not one. A
single session only ever has one query result "in flight" -- interleaving
a write into the middle of the MATCH scan's still-open result forces the
driver to fully buffer whatever remains of the scan into memory first, so
it can run the write, which defeats the whole point of streaming it and
was found the hard way (RSS past 1GB and progress stalled dead after
exactly one batch landed, on a 3.9 GiB droplet). Two sessions multiplex
over separate connections, so the read keeps streaming while writes
happen alongside it.

Usage:

    python migrate_remove_placeholder_labels.py                # dry run
    python migrate_remove_placeholder_labels.py --execute       # deletes

Reads NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD / NEO4J_DATABASE from the
environment, same as server.py / migrate_add_aliases.py.
"""

import os
import sys

import click
from neo4j import GraphDatabase, basic_auth

from discogs_to_neo4j import is_placeholder_label
from progress import Heartbeat

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USERNAME = os.environ.get("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.environ.get("NEO4J_DATABASE", "neo4j")

DELETE_BATCH_QUERY = """
UNWIND $batch AS eid
MATCH (n) WHERE elementId(n) = eid
DETACH DELETE n
"""


def count_labels(session):
    """O(1) via the count store, just for the Heartbeat's ETA."""
    return session.run("MATCH (n:Label) RETURN count(n) AS c").single()["c"]


def find_candidates(session, total):
    """
    Stream every Label's (elementId, name, degree) and yield the ones
    is_placeholder_label flags. degree is fetched here, not recomputed
    later, because after DETACH DELETE the edges are gone -- this is the
    only point the "how much is this actually removing" number is available.
    """
    hb = Heartbeat("scan", total=total, unit="labels")
    hb.begin()
    result = session.run(
        "MATCH (n:Label) RETURN elementId(n) AS eid, n.name AS name, "
        "COUNT { (n)--() } AS degree"
    )
    scanned = 0
    for record in result:  # streamed, not buffered -- same reasoning as export_search_index.py
        scanned += 1
        hb.tick(scanned)
        if is_placeholder_label(record["name"]):
            yield record["eid"], record["name"], record["degree"]
    hb.finish()


def delete_batch(write_session, batch):
    write_session.run(DELETE_BATCH_QUERY, batch=batch).consume()


@click.command()
@click.option('--batch-size', default=5000, show_default=True,
              help='Nodes deleted per DETACH DELETE round trip.')
@click.option('--execute', is_flag=True,
              help="Actually delete. Without this, only scans and reports -- nothing is touched.")
@click.option('--sample-size', default=10, show_default=True,
              help='How many matched names to print as a sanity check.')
def main(batch_size, execute, sample_size):

    if not NEO4J_PASSWORD:
        sys.exit("NEO4J_PASSWORD is not set")

    driver = GraphDatabase.driver(NEO4J_URI, auth=basic_auth(NEO4J_USERNAME, NEO4J_PASSWORD))
    # Two sessions, not one. A single session only ever has one query result
    # "in flight" at a time -- interleaving a write (the DETACH DELETE below)
    # into the middle of the MATCH scan's still-open result forces the driver
    # to fully buffer whatever remains of that 2.4M-row scan into memory
    # before it can run the write, defeating the whole point of streaming it.
    # Found the hard way: memory ballooned past 1GB and progress output
    # stopped dead after exactly one batch actually landed. Separate
    # sessions multiplex over separate connections, so the read keeps
    # streaming while writes happen alongside it.
    read_session = driver.session(database=NEO4J_DATABASE)
    write_session = driver.session(database=NEO4J_DATABASE)
    print(f"connecting to {NEO4J_URI}/{NEO4J_DATABASE}")

    try:
        total = count_labels(read_session)
        matched = 0
        edges_touched = 0
        sample = []
        batch = []

        for eid, name, degree in find_candidates(read_session, total):
            matched += 1
            edges_touched += degree
            if len(sample) < sample_size:
                sample.append(name)
            if execute:
                batch.append(eid)
                if len(batch) >= batch_size:
                    delete_batch(write_session, batch)
                    batch.clear()

        if execute and batch:
            delete_batch(write_session, batch)

        print(f"\n{matched:,} of {total:,} Label nodes matched \"Not On Label\" ({matched/total*100:.2f}%)")
        print(f"{edges_touched:,} edges touched (RELEASED_ON, almost entirely)")
        print("sample of matched names:")
        for name in sample:
            print(f"  {name!r}")

        if execute:
            print(f"\ndeleted {matched:,} nodes and their edges")
        else:
            print("\ndry run: nothing deleted -- pass --execute to actually delete")

    finally:
        read_session.close()
        write_session.close()
        driver.close()


if __name__ == "__main__":
    main()
