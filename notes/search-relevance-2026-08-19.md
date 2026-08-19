# Search relevance: releases don't rank against their own artist, 2026-08-19

Filed while adding live search (`/api/suggest`). Not fixed yet — this is the
diagnosis and a menu of fixes, ranked cheapest first, for whoever picks it up.

## The bug, concretely

`"goldie timeless"` should surface Goldie's 1995 album *Timeless*. It doesn't:

```
CALL db.index.fulltext.queryNodes('entitySearch', 'goldie timeless', {limit: 10})
YIELD node, score RETURN labels(node), node.name, node.title, node.year, score

["Release"], NULL, "Goldie ",  "1964",       7.39   <- matches "goldie" only
["Release"], NULL, "Goldie",   "2012-04-27", 7.39   <- matches "goldie" only
["Artist"],  "GOLDIE GOLDIE", NULL, NULL,     6.99   <- matches "goldie" only
```

The actual node exists and is correctly linked —

```
MATCH (a)-[:CREDITED]->(r:Release) WHERE r.title = 'Timeless' AND a.name CONTAINS 'Goldie'
RETURN a.name, r.title, r.year, elementId(r)
"Goldie", "Timeless", "1995-08-07", "...:13307844"
```

— but it never surfaces, because its own indexed text is just the string
`"Timeless"`. The word "Goldie" lives on a *different* node connected by a
`CREDITED` edge. `entitySearch` indexes `n.name`/`n.title` per node in
isolation; Lucene has no way to know two connected nodes' text belong to the
same "thing" a user is thinking of. With default OR semantics it just ranks
by whichever single term hit hardest, and a release literally titled "Goldie"
wins over the one actually credited to him.

This affects both `/api/search` and the new `/api/suggest` equally — same
index, same query shape.

## Option A — denormalize onto the Release node (the "big" fix)

Add a `creditedNames` (and maybe `labelNames`) property to every `Release`,
concatenating its credited artists'/labels' names, and put it in the
fulltext index alongside `name`/`title`. Then "goldie" and "timeless" are
both indexed on the *same* Lucene document and score together properly.

Two parts, and they're separable:

1. **Live backfill**, no reimport needed — the graph already has the
   `CREDITED`/`RELEASED_ON` edges:
   ```cypher
   MATCH (r:Release)
   OPTIONAL MATCH (r)<-[:CREDITED]-(a)
   OPTIONAL MATCH (r)-[:RELEASED_ON]->(l)
   WITH r, apoc.text.join(collect(DISTINCT a.name), ' ') AS artists,
           apoc.text.join(collect(DISTINCT l.name), ' ') AS labels
   SET r.creditedNames = artists, r.labelNames = labels
   ```
   (needs APOC, or a `reduce()` if it isn't installed) — then `DROP` and
   recreate `entitySearch` over the widened property list. Rebuilding the
   index over ~15M nodes is the expensive part; it took a while on initial
   provisioning and would again here.

2. **Permanent fix in the pipeline**, so it survives the next full rebuild
   instead of being a live-DB-only patch: `discogs_to_neo4j.py`'s
   `process_release` already iterates `<artists>`/`<extraartists>` for the
   `CREDITED` rows, and Discogs' release XML embeds the artist's display
   `<name>` inline in that same block — no second lookup needed. Collect
   those names (and the label names it already resolves) into the extra CSV
   column.

Correct and general, but it's a schema change plus a full-graph reindex on
the live droplet, and a change to a well-exercised part of the transform.

## Option B — boost at query time instead of indexing (the cheap workaround)

Don't touch the index at all. `search`/`suggest` already pull back a small
candidate set (`limit`, or `SUGGEST_LIMIT_MAX` = 15) — cheap enough to widen
that candidate fetch a bit and re-rank in Cypher by checking each
candidate's *neighbours* for the terms the node's own text missed:

```cypher
CALL db.index.fulltext.queryNodes('entitySearch', $term, {limit: 40}) YIELD node, score
OPTIONAL MATCH (node)<-[:CREDITED]-(a) WHERE any(t IN $terms WHERE toLower(a.name) CONTAINS t)
OPTIONAL MATCH (node)-[:RELEASED_ON]->(l) WHERE any(t IN $terms WHERE toLower(l.name) CONTAINS t)
WITH node, score + (CASE WHEN a IS NOT NULL THEN 5 ELSE 0 END)
                  + (CASE WHEN l IS NOT NULL THEN 3 ELSE 0 END) AS boosted
RETURN node ORDER BY boosted DESC LIMIT $limit
```

`$terms` would be `clean_term`'s output lowercased and split on whitespace.
This is a query-time approximation of Option A's effect — a release whose
*neighbour* matches a leftover query term gets pulled up — without a schema
change, a backfill, or a reindex. Cost is one extra relationship hop per
candidate, and the candidate set is already small by construction, so it
should be cheap even before measuring.

Tradeoffs against Option A: only re-ranks within whatever the plain
full-text query already retrieved (so if "goldie" alone doesn't pull the
right release into the top 40 candidates at all, boosting never sees it —
worth checking against a few real queries before trusting it); doesn't help
`CONTAINS`-style substring cases; adds Cypher complexity to `search`/
`suggest` rather than keeping them as thin index lookups.

## Recommendation

Try Option B first — it's an isolated change to `Graph.search`/
`Graph.suggest` in `server.py`, no coordination with the droplet's live index
or the transform pipeline, and it's easy to throw away if it doesn't
actually fix enough real queries. Fall back to Option A only if B's ceiling
turns out too low in practice — and if that happens, do the pipeline change
(A.2) at the same time as the live backfill (A.1), so the backfill isn't
quietly lost on the next full reimport.
