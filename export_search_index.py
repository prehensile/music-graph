#!/usr/bin/env python3
"""
Export a flat (type, id, name, degree, artists, date) CSV straight from
Neo4j, for building a static search index once the intermediate CSVs are
gone.

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

`degree` is each node's total relationship count (`COUNT { (n)--() }`, same
pattern used for hubs in graph_stats.py and for match ranking in server.py),
carried along for step 2 of the note's next-steps: deciding what to trim from
the index needs some measure of a node's prominence, and this is that measure
without a second pass over the graph later. It costs a relationship scan per
node at export time -- there is no shortcut via the count store for
per-node degree, only label totals -- so this export is slower than a plain
id+name pull would be.

`artists` and `date` are both empty for Artist/Group/Label; both are
Release-only, and exist for the same reason: a Release's own title alone
can't tell two same-titled releases apart (there are, unsurprisingly,
several dozen different releases called "Timeless" by different artists
in different years), and the static search index has no other way to show
that, since it carries no relationship data at all otherwise. This is a
*display* fix, not a search/ranking one -- it doesn't help a query find
the right "Timeless", only helps a human reading the results tell them
apart once found. The actual cross-relationship ranking problem
(server.py's _rank_matches neighbour-boost) is unrelated and still has no
client-side answer -- see notes/static-search-downsize-2026-08-19.md.

`date` is `n.year` verbatim -- misleadingly named on the Release node
itself (see CLAUDE.md's "Fixed along the way" for the unrelated labels.csv
header bug this project already hit once from trusting a property name
over its actual content): it holds whatever `<released>` contained in the
XML, which is usually a full date ("2023-12-28") and only sometimes just a
year, not "the year" as a parsed/typed value. Passed through as-is; no
parsing or reformatting attempted here.

`artists` is up to RELEASE_ARTIST_CAP credited names, semicolon-joined --
not Discogs' own credit order, which the graph has no way to reconstruct
(`CREDITED` carries no property distinguishing Discogs' <artists>, the
headline credit, from <extraartists>, session/production credits; both
write to the same relationship type in discogs_to_neo4j.py's
process_release, so that distinction was never preserved, and Cypher gives
no ordering guarantee over OPTIONAL MATCH results to fall back on either).
Two heuristics stand in for it instead, cheaper and more accurate than
either alone:

1. **Prefer Group credits over Artist credits, entirely, when any Group is
   credited.** A release credited to the band Mucc plus ~50 backing/
   session musicians -- individually Artist nodes, not the Group -- is the
   motivating, verified case: with every credited node in one undifferentiated
   pool, arbitrary collect() order surfaced "Yoshio Arimatsu; Jun-ichi
   Yajima; ..." (session players) instead of "Mucc" (the actual band).
   Filtering to Group-labelled credits whenever at least one exists (via
   `EXISTS { (n)<-[:CREDITED]-(:Group) }`, computed once per release, not
   once per candidate) fixes this directly for the common "a band's own
   release, credited alongside its session players" shape, without needing
   any per-candidate degree computation for the (usually many) session
   artists at all -- they're filtered out before COUNT{} ever runs on them.
2. **Within whichever pool that leaves (Group credits if any, else every
   Artist credit), sort by degree descending.** Handles releases with more
   than one Group credited, and every release with no Group credit at all
   (the majority -- most releases are solo/duo, not band releases),
   falling back to "most well-connected first" exactly as `_rank_matches`'
   own tie-break does in server.py.

Both are heuristics, not a real fix -- a genuine one needs `CREDITED` (or a
new relationship) to carry which XML block a credit came from, which is a
discogs_to_neo4j.py + live-migration change, not a search-index one. But
filtering to Group credits first also happens to be the cheaper query:
measured on the 2026-08 dump, computing degree for every CREDITED edge
unconditionally ran at ~1,062 releases/s (would be ~40 minutes for the
Release pass alone, since ~13.7M CREDITED edges exist in total and a
prolific artist's degree got recomputed once per release they're credited
on); filtering most candidates out via EXISTS before any degree computation
runs measured at ~3,976 releases/s instead -- most releases never compute a
single per-candidate degree, since a solo release's one Artist credit still
needs it, but a large compilation's dozens of session-player Artist credits
never do once a Group credit is found among them.

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

# Same cap server.py's Graph.suggest uses for the same collect(DISTINCT ...):
# a handful of names is enough to disambiguate a result, and an unbounded
# collect against a hub-like Various-Artists-credited release would be
# needless cost for no display benefit beyond the first few.
RELEASE_ARTIST_CAP = 3


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

    if label == "Release":
        # OPTIONAL, not MATCH: a release with no CREDITED artist at all
        # (referential drift -- see CLAUDE.md's Known Issues -- or a
        # release whose only credit was a placeholder id filtered out of
        # CREDITED at import) must still flow through with artists="",
        # not drop out of the export entirely.
        #
        # hasGroup is computed once per release, before the candidate
        # OPTIONAL MATCH runs -- not re-evaluated per candidate row, which
        # an EXISTS{} inside the later WHERE would do instead, once per
        # credited node rather than once per release. The WHERE below then
        # keeps only Group candidates when any exist, else every Artist
        # candidate (or the OPTIONAL MATCH's own null placeholder, when a
        # release has no CREDITED node of either kind at all -- hasGroup is
        # false in that case too, so `hasGroup = (cand:Group)` evaluates to
        # `false = null` = null there, which WHERE treats as "exclude", so
        # that placeholder row never reaches the collect() below; the
        # explicit `cand IS NULL OR` keeps it anyway, matching the CASE
        # WHEN cand IS NOT NULL guard the collect() itself still needs for
        # COUNT{} and property access, both to be avoided on a null node
        # rather than relied on to silently do the right thing).
        query = (
            f"MATCH (n:{label}) "
            f"WITH n, EXISTS {{ (n)<-[:CREDITED]-(:Group) }} AS hasGroup "
            f"OPTIONAL MATCH (n)<-[:CREDITED]-(cand) "
            f"WHERE cand IS NULL OR hasGroup = (cand:Group) "
            f"WITH n, collect(DISTINCT "
            f"CASE WHEN cand IS NOT NULL THEN {{name: cand.name, degree: COUNT {{ (cand)--() }} }} END"
            f") AS candInfo "
            f"RETURN n.{id_prop} AS id, n.{name_prop} AS name, candInfo, n.year AS date, "
            f"COUNT {{ (n)--() }} AS degree"
        )
    else:
        query = (
            f"MATCH (n:{label}) RETURN n.{id_prop} AS id, n.{name_prop} AS name, "
            f"COUNT {{ (n)--() }} AS degree"
        )

    result = session.run(query)
    for record in result:  # streamed from the Bolt connection, not buffered
        node_id, name, degree = record["id"], record["name"], record["degree"]
        if not name or not name.strip():
            skipped += 1
            hb.tick(written + skipped)
            continue
        if label == "Release":
            # None entries come from releases with zero CREDITED nodes of
            # either kind (the CASE WHEN in the query above), filtered
            # here rather than in Cypher -- simpler than threading another
            # WHERE through an already-nested WITH chain.
            candidates = sorted(
                (c for c in record["candInfo"] if c is not None),
                key=lambda c: c["degree"], reverse=True,
            )
            artists = "; ".join(c["name"] for c in candidates[:RELEASE_ARTIST_CAP] if c["name"])
            date = record["date"] or ""
        else:
            artists = date = ""
        writer.writerow((label, node_id, name, degree, artists, date))
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
            writer.writerow(("type", "id", "name", "degree", "artists", "date"))
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
