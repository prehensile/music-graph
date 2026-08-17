# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A music knowledge graph built from [Discogs monthly data dumps](https://discogs-data-dumps.s3.us-west-2.amazonaws.com/index.html). The Discogs XML dumps are transformed into CSVs for `neo4j-admin database import`, and a browser-based Sigma.js viewer explores the resulting graph with click-to-expand navigation and natural-language querying via a local Ollama model.

The central engineering problem is scale: the Discogs releases dump is tens of GB of XML in a single file, and masters reference releases by ID, so the pipeline needs random access into a file far too large to hold in memory or parse repeatedly. The solution is to index the releases dump into SQLite first, then stream everything else against that index.

The repository contains only the current path from XML to a Neo4j import. Two earlier approaches to the random-access problem (shelling out to ripgrep per lookup, then a hand-rolled binary search over byte offsets) and a first-pass importer were removed once SQLite superseded them — see [Development history](#development-history) for what they were and why they went.

## Repository Layout

```
.                             # pipeline scripts, run from the repo root
├── xml_split.py              # 1. split a huge XML dump into well-formed chunks
├── releases_to_sqlite.py     # 2. releases XML -> SQLite (id -> raw XML) index
├── merge_sqlite_releases.py  # 3. merge per-chunk SQLite parts into one DB
├── optimise_sqlite_db.py     # 4. ANALYZE the merged DB
├── discogs_to_neo4j.py       # 5. main transform: XML + SQLite -> import CSVs
├── sqlite_to_csv.py          # 6. labels table -> labels.csv
├── dupe_finder.py            # 7. pre-import check for duplicate node IDs
├── neo4j_admin_import.sh     # 8. the bulk import itself
└── web/                      # Sigma.js graph viewer
    ├── index.html
    ├── app.js                # KnowledgeGraphViewer: sigma + physics + Ollama
    ├── styles.css
    └── config.example.js     # copy to config.js and add credentials
```

Data is not in the repo. Scripts assume a `data/` directory holding the dumps, with generated CSVs written into dated subfolders (`data/2025-07-28/`). `.gitignore` excludes `data/`, XML, SQLite and CSV files.

## Why both SQLite and CSV

These are not competing formats and neither replaced the other — they solve different problems at opposite ends of the pipeline.

**CSV is the required output.** `neo4j-admin database import` reads CSV and nothing else, so the pipeline must terminate in CSVs with `:ID`/`:START_ID`/`:END_ID` typed headers. This has never changed and cannot be swapped out while the admin importer is the loading mechanism.

**SQLite appears twice in between, doing two unrelated jobs:**

| | `releases.db` | `labels.db` |
|---|---|---|
| Role | read-only **input index** | write-side **staging table** |
| Built by | `releases_to_sqlite.py` | `discogs_to_neo4j.py` itself |
| Schema | `(id INTEGER PRIMARY KEY, content TEXT)` — `content` is raw XML text | `(id INTEGER PRIMARY KEY, name TEXT, profile TEXT)` — parsed columns |
| Replaced | ripgrep, then binary search over the XML | writing `labels.csv` rows directly |
| CLI flag | `--release-sqlite` (`exists=True`) | `--label-sqlite` (created if absent) |

The asymmetry in those `click.Path` declarations is the quickest way to remember the difference: the release index **must already exist** because the transform only reads it, while the label DB **gets created** because the transform writes it. Matching that, `init_sqlite_files` issues a `CREATE TABLE` for labels but merely opens a cursor for releases.

Per entity, the current flow is:

- **Artists / Groups** — XML → CSV directly. SQLite is never involved.
- **Releases** — SQLite read → CSV. Index in, CSV out.
- **Labels** — SQLite accumulate → CSV, via `sqlite_to_csv.py`.
- **Import** — CSV only, always.

Labels are the only entity that needed a staging table, because they arrive from two sources (the labels dump, and inline `<label>` refs inside release listings) and CSV has no way to revise a row you have already written. SQLite does:

```sql
ON CONFLICT(id) DO UPDATE SET name=excluded.name,
    profile=COALESCE(NULLIF(excluded.profile,''), profile)
```

That upsert keeps whichever record is richer, which is why labels need a separate export step instead of being written straight out — the dedupe has to happen somewhere that supports updates.

## The Pipeline

Run in this order. Steps 1–4 build the releases index and only need redoing when a new dump lands.

**Requires Python 3.12+** — `discogs_to_neo4j.py` and `sqlite_to_csv.py` use `csv.QUOTE_STRINGS`, added in 3.12. On 3.11 and earlier they fail with `AttributeError: module 'csv' has no attribute 'QUOTE_STRINGS'`.

```bash
# 1. Split the releases dump into N chunks, each a well-formed XML document.
#    Chunks are cut on </release> boundaries and given synthetic root tags.
python xml_split.py data/discogs_20250201_releases.xml release 16 releases

# 2. Index each chunk into its own SQLite DB (run these in parallel).
python releases_to_sqlite.py --xml-file data/discogs_20250201_releases.xml.1 \
                             --db-path data/releases_part1.db

# 3. Merge the per-chunk DBs into one.
python merge_sqlite_releases.py --pattern "data/releases_part*.db" \
                                --output data/releases.db --method streaming

# 4. Build query statistics on the merged DB.
python optimise_sqlite_db.py data/releases.db

# 5. Transform artists, masters and labels into Neo4j import CSVs.
#    Masters are resolved to their main_release against the SQLite index.
#    The output folder must be empty -- see Known Issues.
python discogs_to_neo4j.py \
    --artist-xml data/discogs_20250201_artists.xml \
    --master-xml data/discogs_20250201_masters.xml \
    --label-xml  data/discogs_20250201_labels.xml \
    --release-sqlite data/releases.db \
    --label-sqlite   data/labels.db \
    --output-folder  data/2025-07-28/

# 6. Flatten the deduplicated labels table to CSV, overwriting the placeholder
#    labels.csv that step 5 leaves behind.
python sqlite_to_csv.py --sqlite-path data/labels.db --output-folder data/2025-07-28/

# 7. Sanity-check node CSVs for duplicate IDs before importing.
python dupe_finder.py data/2025-07-28/artists.csv

# 8. Bulk import (Neo4j must be stopped).
./neo4j_admin_import.sh data/2025-07-28 discogs

# 9. Serve the viewer.
cp web/config.example.js web/config.js   # then fill in credentials
cd web && python -m http.server 8000
```

### Why the shape of step 5

`discogs_to_neo4j.py` deliberately never iterates the releases dump — it has no `--release-xml` option at all. Releases are reached only through masters: for each `<master>` it reads `<main_release>` and fetches that one release from the SQLite index. This keeps a single release per master — the canonical pressing rather than every reissue — which is the graph the project actually wants. Passing `--master-xml` without `--release-sqlite` is rejected up front.

## Data Model

| Node | Source | Key properties |
|---|---|---|
| `Artist` | `<artist>` with no `<members>` | `artistId:ID(Artist)`, `name`, `realname`, `profile` |
| `Group` | `<artist>` **with** `<members>` | `groupId:ID(Group)`, `name`, `profile` |
| `Release` | `<release>` reached via a master's `main_release` | `releaseId:ID(Release)`, `year`, `title` |
| `Label` | `<label>`, plus inline refs inside releases | `labelId:ID(Label)`, `name`, `profile` |

| Relationship | CSV | Direction |
|---|---|---|
| `MEMBER_OF` | `artist_group_links.csv` | Artist → Group |
| `CREDITED` | `artist_release_links.csv` | Artist → Release (from `<artists>` and `<extraartists>`) |
| `RELEASED_ON` | `release_label_links.csv` | Release → Label |
| `SUBLABEL` | `label_sublabel_links.csv` | sublabel Label → parent Label |

`Artist` and `Group` are separate Neo4j ID spaces, both drawn from the same Discogs ID sequence — a given Discogs artist ID lands in exactly one of `artists.csv` or `groups.csv` depending on whether it has `<members>`. Relationship rows must therefore point into the right space; getting this backwards produces dangling edges that the importer rejects rather than a silently wrong graph.

**Masters are an intermediate hop, not a node.** There is no `Master` label in the graph. `process_master` exists only to resolve `main_release` and hand the resulting release to `process_release`.

## Web Interface

`web/app.js` is a single `KnowledgeGraphViewer` class loading graphology, Sigma.js 2.4 and the Neo4j web driver from unpkg. No build step — open it with any static server.

- **Querying.** Input goes through `convertNaturalLanguageToCypher`. A regex fast-path treats anything that looks like a bare name as a substring search and skips the LLM entirely; anything else is sent to Ollama (`llama3.2`) with a prompt containing the schema and few-shot examples, and falls back to a hand-written `CONTAINS` query if Ollama is unreachable.
- **Custom physics.** `updatePhysics` is a hand-rolled force simulation rather than an off-the-shelf layout: mass-scaled inverse-square repulsion, spring attraction along edges toward an ideal distance of 100, a weak pull to the origin, and velocity damping. `handleCollisions` adds O(n²) circle collision with positional separation and elastic impulse response.
- **Degree drives appearance.** Queries return `count(DISTINCT ...) as <key>_degree` alongside the nodes; `renderGraphData` picks up any key ending in `_degree`. Size is logarithmic (`calculateNodeSize`), physics mass is linear (`calculateNodeMass`), so hubs are both bigger and harder to shove.
- **Expansion.** Clicking a node runs `expandNode`, which re-renders additively with `parentNodeId` set so new neighbours spawn in a ring just outside the parent's radius instead of at random coordinates. A `wasDragging` flag stops a drag from registering as a click.
- **Panning is off** (`enableEdgePanning`/`enableNodePanning` false) so dragging always means moving a node.

The viewer was written against an early two-label version of the graph and has not caught up with the schema. `getNodeColor` covers `Artist` and `Master` plus generic labels (`Person`, `Organization`, …) left over from whatever template this started as; `Group`, `Release` and `Label` fall through to default grey. The Ollama prompt still describes the graph as containing "Artist and Master nodes", and the default query hardcodes `goldie` as a test subject. `Master` no longer exists as a node at all.

## Development history

Reconstructed from the file dates in the original archives and from what the code showed. Useful for reading intent in the surviving code; the superseded files themselves are recoverable from commit `bdd80c1`, which imported the project verbatim before this cleanup.

**1. First pass — artists and masters only** (`neo4j_bulk_import.py` / `.sh`, 21–28 Jun 2025, *removed*). Stdlib `xml.etree`, positional `sys.argv`, hardcoded output filenames. Handled just artists and masters, skipping artist ID 194 (*"Various"*) with the comment *"fucks things up"*. Its shell script imported `Artist` and `Master` nodes and one `RELEASED` relationship.

**2. The viewer** (30 Jun – 1 Jul). Built against that first two-label dataset, which explains every schema anachronism listed above. The physics engine and click-to-expand behaviour all date from here.

**3. Generalising the transform** (by 14 Jul). `discogs_to_neo4j.py` replaced the first pass: `click` CLI, `lxml` for XPath and `getparent()`, all node types, per-type writer setup. Element lookup shelled out to `rg -m1 -A200` per release.

**4. Binary search over the XML** (`xml_binary_search.py`, 15 Jul, *removed*). Ripgrep meant a fresh scan of a multi-GB file per lookup. `XMLBinarySearcher` exploited the fact that Discogs dumps are **sorted by ID**: seek to a byte offset, scan forward to the next element boundary, read its ID, and lazily build a binary tree of offset→ID while bisecting. It carried the scars of being hard to get right — visited-node loop detection, a 50-iteration cap, a linear-search finish within 10 IDs of the target, and a widened fallback for IDs that turned out not to exist.

**5. SQLite index instead** (17–18 Jul). Binary search worked but stayed slow and fragile, so the releases dump got indexed properly: one row per release, `id` → raw XML. To make that ingest tractable it was parallelised — `xml_split.py` cuts the dump on `</release>` boundaries and wraps each chunk in synthetic root tags so it parses standalone, and `merge_sqlite_releases.py` recombines the parts. This is the approach that stuck, and the one the repo now contains exclusively.

**6. Index tuning** (`optimise_sqlite_db.py`, 22 Jul). A three-line `ANALYZE`, with `VACUUM` commented out — unsurprising on a database this size.

**7. The duplicate hunt** (`dupe_finder.py`, 28 Jul). `neo4j-admin` rejects duplicate node IDs, so this scans a node CSV for repeated IDs in column 0. The saved output from that run recorded **30,655 duplicate artist IDs**, and its shape was diagnostic: every ID appeared exactly twice, with the second copies in an unbroken run of lines 30656–61310. Not scattered corruption — the same 30,655-row block written twice, because `open_writer` opens CSVs in append mode. See Known Issues; the saved log was removed as a stale artifact.

**8. The real import** (`neo4j_admin_import.sh`, 29 Jul). The full schema at last: `Artist`, `Group`, `Label`, `Release` nodes and `MEMBER_OF`, `CREDITED`, `SUBLABEL`, `RELEASED_ON` relationships.

**9. Fixing labels properly** (31 Jul – 1 Aug). The label duplicates got a real fix rather than a workaround: route labels through SQLite with a merging upsert, then export once with `sqlite_to_csv.py`.

The arc: naive streaming parse → generalise → ripgrep random access → hand-rolled binary search → SQLite index with parallel ingest → fight duplicates → dedupe in SQL. Throughout, CSV remained the output format; SQLite changed how the input was reached and how writes were staged, never what the importer consumed.

## Known Issues and Gotchas

- **`open_writer` appends.** CSVs are opened `"a"` with the header re-written each call, so re-running the transform into a folder that already holds output duplicates every row and injects headers mid-file. `discogs_to_neo4j.py` now **refuses to start** if the output folder contains any `.csv`, rather than silently corrupting it. The underlying append mode is unchanged — switching to `"w"` would clobber output on partial re-runs, so the guard is deliberately a refusal, not an overwrite.
- **Sublabels can dangle.** Sublabels are written to `label_sublabel_links.csv` but not to `labels.csv`, on the assumption that every sublabel also appears as a top-level `<label>` in the labels dump. True for full dumps, not for partial ones; add `--skip-bad-relationships=true` if the import aborts on these.
- **`labels.csv` is written twice.** Step 5 creates a header-only placeholder whenever `--label-xml` is given; step 6 overwrites it with the real export. Running step 5 without step 6 leaves an empty label set and dangling `RELEASED_ON` edges.
- **`expandNode` interpolates `id(center) = ${nodeId}` into Cypher**, and the name fast-path in `convertNaturalLanguageToCypher` interpolates user input into a string literal. Fine for a local single-user tool, not safe to expose.
- **`id()` is deprecated** in current Neo4j in favour of `elementId()`; the viewer relies on `identity` / `id()` throughout.
- **Viewer colours are incomplete** — see Web Interface.

### Fixed during the cleanup

Recorded here because the symptoms are non-obvious if you hit them in an older checkout:

- **`MEMBER_OF` edges were inverted.** `process_artist` wrote `[group_id, member_id]` under the header `:START_ID(Artist),:END_ID(Group)`. Since a group's ID lives in `groups.csv` and a member's in `artists.csv`, *both* columns pointed at the wrong ID space and every `MEMBER_OF` row was dangling in both directions — the importer rejected all of them.
- **`labels.csv` headers were wrong.** Both writers emitted `["labelId:ID(Label)", "year", "title"]` while the table columns are `(id, name, profile)`, so `Label.year` held the name and `Label.title` held the profile.
- **`neo4j_admin_import.sh` ended in `--overwrite-destinationr`** and hardcoded `/Users/henry/...` paths. It now takes the CSV folder and database name as arguments and preflights that every expected CSV exists.
- **Header-only `masters.csv` / `artist_master_links.csv`** were created on every run and never written to. Those writers are gone.

## Dependencies and Services

### Python 3.12+
`lxml`, `click`, `alive_progress`; `sqlite3`, `csv`, `xml.etree` from the stdlib. The two XML libraries are not interchangeable here — `discogs_to_neo4j.py` needs `lxml` for `getparent()` and `xpath()`, while `releases_to_sqlite.py` uses stdlib `ElementTree`.

```bash
pip install lxml click alive_progress
```

### External services
- **Neo4j** on `bolt://localhost:7687`. Must be **stopped** for `neo4j-admin database import`, which writes into a database named `discogs` by default.
- **Ollama** on `http://localhost:11434` serving `llama3.2`, for natural-language → Cypher. Optional: the viewer degrades to a substring query if it is unavailable.

### Web (all from unpkg CDN, no build step)
graphology 0.25.4, Sigma.js 2.4.0, neo4j-driver 5.12.0.

### Credentials
`web/config.js` is gitignored; copy `web/config.example.js` and fill in `neo4jPassword`. `app.js` reads `window.MUSIC_GRAPH_CONFIG` and shows an error if no password is set. Do not hardcode credentials back into `app.js` — an earlier revision did, and the password was scrubbed when this repo was first pushed.
