# Downsizing to a static, browser-only viewer, 2026-08-19

Exploratory only — nothing implemented. Filed so the sizing numbers and the
options considered aren't re-derived from scratch next time this comes up.
Distinct from `droplet-downsize-2026-08-19.md`, which shrinks the *droplet*
running the existing Neo4j+`server.py` stack; this note is about dropping
that stack entirely in favour of static hosting.

## The question

Could the viewer's search be served with no backend at all — static files
only, search computed in the browser — instead of Neo4j + `server.py` on a
droplet?

## Why a naive approach doesn't fit

The obvious first move, an SQLite `(id, name)` lookup table for every node,
was sized empirically (synthetic benchmark matching the real dump's node mix
and name-length distribution, measured then scaled x10 — not just estimated
from page-overhead arithmetic):

| Variant | Size at ~15.2M nodes |
|---|---|
| plain `(id INTEGER PRIMARY KEY, name TEXT)`, no index | ~420–480 MB |
| + `CREATE INDEX` on `name` | ~870 MB |
| FTS5 virtual table | ~1.15 GB |

Node mix used (from `notes/handover-2026-08-18.md`'s real 2026-08 dump):
10,163,318 Artist+Group, 2,579,769 Release, ~2.4–2.7M Label.

All three are "shippable" in the sense that a static host doesn't care, but
none of them is "casual use" — the browser would need to pull down
hundreds of MB to a GB before a single search resolves, once (or on every
uncached visit).

## The shape that actually fits: trim, then shard, don't compress harder

Three levers, cheapest/most-valuable first:

1. **Trim the corpus.** Releases (2.6M nodes) are reachable by tapping their
   credited artist in the graph already, so a standalone release entry in
   the search index is mostly redundant. Dropping releases, and maybe
   low-degree nodes generally, is free — a data decision, not an engineering
   one — and could cut the indexed set from ~15M to ~10M before anything
   else changes.
2. **Shard the name index by prefix into small static files**, fetched on
   demand as the user types (2–3 char prefix → one small JSON file, tens of
   KB, not the whole corpus). This mirrors the debounce +
   stale-response-discard pattern already in `web/app.js`'s
   `fetchSuggestions`/`suggestToken` — same UI logic, just swapping the
   network call from `/api/suggest` to a static file `fetch()`.
3. **Let the static host gzip/brotli automatically** — falls out of small
   shard files for free, no separate step.

Explicitly *not* pursued as first choice: shipping the whole SQLite file to
a `sql.js`/wasm runtime in the browser (either loads the whole DB, defeating
the point, or needs partial-read-over-HTTP-Range plumbing — more cleverness
than the payoff justifies here); FST/trie index compression (space-optimal
but a build step and runtime to maintain beyond what sharding already buys).

## Candidate library: Pagefind

[Pagefind](https://pagefind.app) (CloudCannon, Rust/WASM) implements this
exact pattern already — build-time indexer, index split into chunk files,
browser fetches only the chunks relevant to the current query. Closest
off-the-shelf match to hand-rolling the shard scheme above.

Caveat: Pagefind's default mode crawls a folder of built HTML pages. This
dataset is flat `(id, name, type)` records, not pages, so it'd need
Pagefind's Node.js indexing API for custom records rather than the HTML-
crawling CLI. Exact current API surface for that (method names, whether it
still calls it "custom records") needs checking against Pagefind's docs
before committing — noted from general knowledge of the project, not just
having read its reference.

Fallback if the WASM runtime is unwanted: **FlexSearch** (pure JS) supports
exporting an index in balanced parts for manual lazy-loading — more DIY
wiring, no WASM dependency.

## Update: the CSVs won't be around to use

The intermediate import CSVs (`artists.csv`/`groups.csv`/`releases.csv`/
`labels.csv`) are going away as part of the droplet downsize — see
`droplet-downsize-2026-08-19.md`. That's fine: a flat export straight from
Neo4j is a better source anyway, since it reflects the graph as actually
imported (post-dedup, post placeholder-artist filtering) rather than the
pre-import inputs, and it's still available for as long as the Neo4j
instance is, independent of what happens to the CSVs.

Wrote `export_search_index.py` for this: streams `(type, id, name)` for
every `Artist`/`Group`/`Release`/`Label` node straight off the Bolt
connection (no buffering — memory stays flat regardless of the ~15.2M node
count), writes plain or gzip CSV depending on the output extension, skips
rows with an empty name, and uses each node's **Discogs-native id**
(`artistId`/`groupId`/`releaseId`/`labelId`) rather than Neo4j's `elementId`
— the native id survives a dump/load migration or a future reimport, which
is the whole point of exporting it now. `Release` and `Label` are separate
id spaces from each other and from the shared Artist/Group `Entity` space,
so `id` alone isn't globally unique — every row carries `type`, and any
consumer of the export has to key on `(type, id)`, not `id` alone.

Not yet run. Should be run against the live droplet, output kept somewhere
durable (e.g. alongside the `neo4j-admin database dump` backup mentioned
below), before the CSVs actually disappear.

```
NEO4J_PASSWORD=... python export_search_index.py -o search_export.csv.gz
```

Also worth keeping regardless: the `neo4j-admin database dump` binary
backup already produced for the droplet migration (~1.09 GB compressed,
per `droplet-downsize-2026-08-19.md`) is cheap insurance in case the search
index ends up needing richer fields later (bios, credited-artist names for
the neighbour-boost — see below) beyond flat id+name. It just needs a
running Neo4j to read back, unlike the flat export.

## Next steps, when this gets picked back up

1. Run `export_search_index.py` against the live droplet and stash the
   output somewhere durable before the old droplet/CSVs are gone.
2. Decide what actually gets trimmed from the ~15.2M nodes (releases-only
   cut vs. also degree-based pruning) — affects both index size and whether
   search UX loses anything users cared about. Can be done as a filter over
   the export rather than needing to touch Neo4j again.
3. Pull Pagefind's current Node API docs, confirm the custom-record path.
4. Sketch the build step against the export from step 1.
5. Whatever replaces `_rank_matches`' neighbour-boost (see
   `search-relevance-2026-08-19.md`) client-side needs its own answer —
   Pagefind's own ranking won't know about `CREDITED`/`RELEASED_ON` edges
   any more than `entitySearch` did.
