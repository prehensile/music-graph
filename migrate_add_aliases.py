#!/usr/bin/env python3
"""
Add ALIAS_OF relationships to an already-imported graph, without a full
neo4j-admin re-import.

Discogs' <aliases> block -- id/read-only links between artist records credited
as the same act under different names (Jah Wobble / John Wardle is the
motivating case) -- was never parsed by discogs_to_neo4j.py: process_artist
only read <members>/<realname>/<profile>. This script reuses the same
extraction now used by the transform (see iter_artist_aliases in
discogs_to_neo4j.py) and MERGEs the relationship straight into a live
database over bolt, so an existing build doesn't need to be torn down and
reimported from scratch just to pick up one new edge type.

Usage:

    python migrate_add_aliases.py --artist-xml data/discogs_20260801_artists.xml.gz

Reads NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD / NEO4J_DATABASE from the
environment, same as server.py. Idempotent: MERGE means re-running (e.g. after
a partial run, or once against every future monthly dump) never duplicates an
edge already there.
"""

import os
import sys
import traceback
import click

from neo4j import GraphDatabase, basic_auth

from progress import Heartbeat
from discogs_to_neo4j import stream_elements, iter_artist_aliases


NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USERNAME = os.environ.get("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.environ.get("NEO4J_DATABASE", "neo4j")

# Same shared ID space as MEMBER_OF/CREDITED (see the Data Model section of
# CLAUDE.md): an alias id may belong to either label, and which one is not
# knowable from the XML alone. coalesce() picks whichever side actually
# matched.
MERGE_BATCH_QUERY = """
UNWIND $batch AS pair
OPTIONAL MATCH (a1:Artist {artistId: pair.a})
OPTIONAL MATCH (a2:Group {groupId: pair.a})
OPTIONAL MATCH (b1:Artist {artistId: pair.b})
OPTIONAL MATCH (b2:Group {groupId: pair.b})
WITH pair, coalesce(a1, a2) AS a, coalesce(b1, b2) AS b
FOREACH (_ IN CASE WHEN a IS NOT NULL AND b IS NOT NULL THEN [1] ELSE [] END |
    MERGE (a)-[:ALIAS_OF]->(b)
)
RETURN
    count(CASE WHEN a IS NOT NULL AND b IS NOT NULL THEN 1 END) AS matched,
    count(CASE WHEN a IS NULL OR b IS NULL THEN 1 END) AS unmatched
"""


def ensure_indexes(session):
    """
    Neither artistId nor groupId is indexed after a neo4j-admin import -- the
    ID space named in the CSV header (e.g. "artistId:ID(Entity)") only exists
    to resolve relationship endpoints during the bulk load, and is not kept as
    a queryable index afterwards. Without one, MERGE_BATCH_QUERY's property
    lookups would be full label scans per pair, over ~10M Entity nodes --
    infeasible at the pair counts here. Created once, IF NOT EXISTS, so
    re-running this script is free after the first time.
    """
    session.run("CREATE INDEX artist_artistId IF NOT EXISTS FOR (n:Artist) ON (n.artistId)")
    session.run("CREATE INDEX group_groupId IF NOT EXISTS FOR (n:Group) ON (n.groupId)")
    session.run("CALL db.awaitIndexes(1800)").consume()


def flush_batch(session, batch, stats):
    if not batch:
        return
    record = session.run(MERGE_BATCH_QUERY, batch=batch).single()
    stats["matched"] += record["matched"]
    stats["unmatched"] += record["unmatched"]


@click.command()
@click.option('--artist-xml', required=True, type=click.Path(exists=True),
              help='Path to the artist XML dump (.xml or .xml.gz) -- the same file discogs_to_neo4j.py was given.')
@click.option('--batch-size', default=2000, show_default=True,
              help='Alias pairs per MERGE round trip.')
@click.option('--dry-run', is_flag=True,
              help="Parse and dedupe the XML, print counts, but don't touch the database.")
def main(artist_xml, batch_size, dry_run):

    if not dry_run and not NEO4J_PASSWORD:
        sys.exit("NEO4J_PASSWORD is not set")

    driver = None
    session = None
    if not dry_run:
        driver = GraphDatabase.driver(NEO4J_URI, auth=basic_auth(NEO4J_USERNAME, NEO4J_PASSWORD))
        session = driver.session(database=NEO4J_DATABASE)
        print(f"connecting to {NEO4J_URI}/{NEO4J_DATABASE}")
        ensure_indexes(session)

    # Shared for the whole streaming pass so each unordered pair is only ever
    # queued once, exactly as discogs_to_neo4j.process_artist does -- Discogs
    # records an alias on both sides, and a naive pass would otherwise MERGE
    # the same edge twice (harmless, since MERGE is idempotent, but doubles
    # the round trips for nothing).
    seen_pairs = set()
    batch = []
    stats = {"matched": 0, "unmatched": 0, "declared": 0}
    beat = Heartbeat("alias", unit="pairs queued")
    beat.begin()

    def on_artist(element):
        # Mirrors process_artist's own defensiveness: one malformed record
        # (a missing <id>, a non-numeric alias id attribute) must not abort a
        # pass over 10M+ records. iter_artist_aliases itself assumes a valid
        # <id>, so failures land here rather than deeper in.
        try:
            element_id = element.find("id")
            if element_id is None or element_id.text is None:
                return
            for self_id, alias_id in iter_artist_aliases(element):
                stats["declared"] += 1
                key = (self_id, alias_id) if int(self_id) < int(alias_id) \
                    else (alias_id, self_id)
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                batch.append({"a": key[0], "b": key[1]})
                beat.tick(count=len(seen_pairs))
                if not dry_run and len(batch) >= batch_size:
                    flush_batch(session, batch, stats)
                    batch.clear()
        except Exception:
            traceback.print_exc()
            stats["errors"] = stats.get("errors", 0) + 1

    stream_elements(artist_xml, "artist", on_artist)

    if not dry_run:
        flush_batch(session, batch, stats)

    beat.finish()

    print(f"  {stats['declared']:,} alias entries declared in the XML")
    print(f"  {len(seen_pairs):,} distinct unordered pairs")
    if dry_run:
        print("  dry run: nothing written")
    else:
        print(f"  {stats['matched']:,} pairs merged as ALIAS_OF")
        if stats["unmatched"]:
            pct = stats["unmatched"] / len(seen_pairs) * 100
            print(f"  {stats['unmatched']:,} pairs skipped ({pct:.3f}%): "
                  f"one or both ids have no Artist/Group node in this graph")
    if stats.get("errors"):
        print(f"  {stats['errors']:,} artist records raised an error and were skipped -- see traceback above")

    if session:
        session.close()
    if driver:
        driver.close()


if __name__ == "__main__":
    main()
