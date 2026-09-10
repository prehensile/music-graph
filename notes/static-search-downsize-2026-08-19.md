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

## Update 2026-09-07: export run, and a decision not to trim

`export_search_index.py` gained a `degree` column (`COUNT { (n)--() }`, same
pattern as the hubs query in `graph_stats.py` and match ranking in
`server.py`) and was run against the live droplet:

```
NEO4J_PASSWORD=... python export_search_index.py -o search_index_export.csv.gz
```

15,163,915 rows written (2 skipped for empty name), matching the count
store exactly (Artist 9,498,432 + Group 664,886 + Release 2,579,769 + Label
2,420,830 = 15,163,917, less the 2 empty-name skips). 201.8 MB compressed /
543.4 MB uncompressed, ~18 minutes end to end (Artist alone took 11m45s at
~13.5k rows/s — degree costs a relationship scan per node, so this export
is slower than a plain id+name pull would have been). Output kept at
`search_index_export.csv.gz` alongside the repo.

**Decision: don't trim.** Step 2 below (releases-only cut, maybe
degree-based pruning) is explicitly not being pursued for now — keep the
full ~15.16M rows and let sharding alone do the work of keeping any one
fetch small. `degree` stays in the export regardless, since it's free to
carry once fetched and may still matter for client-side ranking (see step 5
below) even without trimming.

## Next steps, when this gets picked back up

1. ~~Run `export_search_index.py` against the live droplet and stash the
   output somewhere durable before the old droplet/CSVs are gone.~~ Done,
   see above.
2. ~~Decide what actually gets trimmed from the ~15.2M nodes.~~ Decided:
   nothing — see above.
3. Pull Pagefind's current Node API docs, confirm the custom-record path.
   (Superseded for now — see `shard_search_index.py` below, which
   hand-rolls the scheme instead of adopting Pagefind. Revisit only if the
   hand-rolled version proves not worth maintaining.)
4. Sketch the build step against the export from step 1. Done —
   `shard_search_index.py`, see below.
5. Whatever replaces `_rank_matches`' neighbour-boost (see
   `search-relevance-2026-08-19.md`) client-side needs its own answer —
   Pagefind's own ranking won't know about `CREDITED`/`RELEASED_ON` edges
   any more than `entitySearch` did, and neither does the hand-rolled
   shard scheme as built. Still open.
6. Not wired into `app.js` yet — the shards exist on disk but nothing
   serves or fetches them from the viewer. Still open.

## `shard_search_index.py`

Written to shard the full (untrimmed) export into small per-prefix JSON
files a browser can fetch on demand, per the "shape that actually fits"
section above. Two-phase, so peak memory stays flat regardless of the
15.16M-row input — worth being deliberate about on this droplet
specifically: it's the downsized `s-2vcpu-4gb` box (`free -h` shows 3.8 GiB
total, ~1.2 GiB available at the time this ran), and holding all 15.16M
rows as Python tuples at once would plausibly have run into several GB by
itself.

- **Phase 1 (partition):** streams the export once, normalises each name
  (Unicode NFKD decompose + strip combining marks + casefold, so e.g.
  "Björk"/"Bjork" land in the same bucket), and appends each row as a
  compact JSON line to one of `--partitions` (default 64) bucket files,
  chosen by hashing the row's normalised 2-char prefix. Every row for a
  given prefix always lands in the same partition file, but each partition
  file holds many different prefixes — so partition count stays fixed
  regardless of how skewed the real prefix distribution is (a lot of
  Discogs artist names cluster under a handful of Latin-alphabet prefixes;
  this is what keeps any one partition from blowing up).
- **Phase 2 (finalise):** each partition file is now small enough
  (~1/64th of the total) to load into memory whole. Rows are grouped by
  exact prefix; any prefix over `--max-shard-rows` (default 500) is
  recursively split one character deeper, up to `--max-prefix-len`
  (default 6), so a very common prefix (e.g. "the") still ends up as
  several small shards rather than one large one. Each final shard is
  written as compact JSON — an array of `[type, id, name, degree]` rows,
  not objects, since repeating four key names across 15M rows would cost
  real bytes for nothing — alongside `manifest.json`, which lists every
  shard's prefix, filename and row count so the browser can find the right
  shard for a typed query without ever fetching an index of the whole
  corpus.

```
python shard_search_index.py search_index_export.csv.gz -o web/search-index
```

## Update 2026-09-07 (later same day): "Not On Label", and two build bugs found running it for real

First real run (`--max-shard-rows 500 --max-prefix-len 6`, the coded defaults at the time) produced 321,649 shards from the full 15,163,915-row export, but exposed two problems, both fixed:

1. **One shard held 314,180 rows** — prefix `"not on"`, from Discogs' `"Not On Label (Artist Self-released)"` convention for unofficial releases. `--max-prefix-len 6` capped recursion before the differentiating text (the artist name, well past character 6) ever kicked in. Fixed by raising the defaults (`--max-shard-rows 2000 --max-prefix-len 24`); re-running dropped that branch to 3,624 shards (largest 1,992 rows) and cut the total to 210,860 shards.
2. That surfaced a bigger, separate problem: **13% of all Label nodes (313,957 of 2,420,829) were "Not On Label" placeholders**, not real labels — same shape as `PLACEHOLDER_ARTIST_IDS` (194/355) already filtered out of `CREDITED`, just id-per-occurrence rather than one or two fixed ids, so name-matched instead (`is_placeholder_label` in `discogs_to_neo4j.py`). Fixed at all three levels rather than just the search index — see CLAUDE.md's Data Model section and `migrate_remove_placeholder_labels.py`:
   - `discogs_to_neo4j.py` now refuses to write these going forward (`upsert_label` + the `RELEASED_ON` gate in `process_release`).
   - `migrate_remove_placeholder_labels.py` deleted the 313,957 nodes (109,030 + a first test batch of 5,000 + 313,957 total across two runs — see below) and their 157,130 edges (136,439 `RELEASED_ON`, 20,691 `SUBLABEL`, the latter against empty-named orphan nodes from a malformed nested `<sublabels>` block, not real hierarchy) from the live droplet.
   - The search index gets these dropped for free now, just by re-exporting from the cleaned graph — no filtering logic needed in `export_search_index.py`/`shard_search_index.py` themselves.

Along the way, the migration script itself had a real bug, caught mid-run rather than in review: it shared one Bolt session between the streaming `MATCH` scan and the interleaved `DETACH DELETE` batches. Interleaving a write into a still-open read result forces the Neo4j Python driver to buffer the rest of that read into memory before it can run the write — on this droplet (`s-2vcpu-4gb`, per `droplet-downsize-2026-08-19.md`) that meant RSS past 1GB and progress stalling dead after exactly one 5,000-row batch had landed. Fixed by giving the script two sessions, one for reads and one for writes (see `migrate_remove_placeholder_labels.py` and the note in CLAUDE.md's Migrating an existing database section). The one batch that landed before the hang was clean (a complete, committed `DETACH DELETE`, not a partial/corrupt state) — confirmed by checking the live count store before re-running the fixed version for the remaining rows.

`search_index_export.csv.gz` and `web/search-index/` are being regenerated against the now-cleaned graph as of this update; expect both to shrink by roughly the 13% Label cut once that finishes.
