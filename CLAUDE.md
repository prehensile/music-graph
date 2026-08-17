# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A music knowledge graph built from [Discogs monthly data dumps](https://discogs-data-dumps.s3.us-west-2.amazonaws.com/index.html). The Discogs XML dumps are transformed into CSVs for `neo4j-admin database import`, and a browser-based Sigma.js viewer explores the resulting graph with click-to-expand navigation and natural-language querying via a local Ollama model.

The central engineering problem is scale: the Discogs releases dump is tens of GB of XML in a single file, and masters reference releases by ID, so the pipeline needs random access into a file far too large to hold in memory or parse repeatedly. Most of the code here exists to solve that problem, and the repository contains three successive attempts at it (see [Development history](#development-history)).

## Repository Layout

```
.                          # ingest / transform scripts, run from the repo root
├── xml_split.py           # split a huge XML dump into well-formed chunks
├── releases_to_sqlite.py  # releases XML -> SQLite (id -> raw XML) index
├── merge_sqlite_releases.py  # merge per-chunk SQLite parts into one DB
├── optimise_sqlite_db.py  # ANALYZE the merged DB
├── xml_binary_search.py   # XMLBinarySearcher: random access into sorted XML
├── discogs_to_neo4j.py    # main transform: XML/SQLite -> Neo4j import CSVs
├── sqlite_to_csv.py       # labels table -> labels.csv
├── dupe_finder.py         # find duplicate node IDs in a generated CSV
├── neo4j_admin_import.sh  # the real bulk import (all node + rel types)
├── neo4j_bulk_import.py   # superseded first-pass importer (artists/masters)
├── neo4j_bulk_import.sh   # superseded first-pass import command
├── artist_dupes.txt       # saved dupe_finder.py output from the 2025-07-28 run
└── web/                   # Sigma.js graph viewer
    ├── index.html
    ├── app.js             # KnowledgeGraphViewer: sigma + physics + Ollama
    ├── styles.css
    └── config.example.js  # copy to config.js and add credentials
```

Data is not in the repo. Scripts assume a `data/` directory holding the dumps, with generated CSVs written into dated subfolders (`data/2025-07-28/`) and hand-assembled sets in `data/aggregated/`. `.gitignore` excludes `data/`, XML, SQLite and CSV files.

## The Pipeline

Run in this order. Steps 1–4 build the releases index and only need redoing when a new dump lands.

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
#    Labels are upserted into SQLite rather than written straight to CSV.
python discogs_to_neo4j.py \
    --artist-xml data/discogs_20250201_artists.xml \
    --master-xml data/discogs_20250201_masters.xml \
    --label-xml  data/discogs_20250201_labels.xml \
    --release-sqlite data/releases.db \
    --label-sqlite   data/labels.db \
    --output-folder  data/2025-07-28/

# 6. Flatten the deduplicated labels table to CSV.
python sqlite_to_csv.py --sqlite-path data/labels.db --output-folder data/2025-07-28/

# 7. Sanity-check node CSVs for duplicate IDs before importing.
python dupe_finder.py data/2025-07-28/artists.csv

# 8. Bulk import (Neo4j must be stopped; edit the absolute paths first).
./neo4j_admin_import.sh

# 9. Serve the viewer.
cp web/config.example.js web/config.js   # then fill in credentials
cd web && python -m http.server 8000
```

### Why the shape of step 5

`discogs_to_neo4j.py` deliberately does **not** iterate the releases dump. Its main loop skips the `release` node type (`node_type != "release"`) and reaches releases only through masters: for each `<master>` it reads `<main_release>` and fetches that release by ID. This keeps one release per master — the canonical pressing rather than every reissue — which is the graph the project actually wants.

Label handling is the other subtlety. Labels arrive from two places: the labels dump, and inline `<label>` references inside release listings. `process_label` distinguishes them by checking for a `release` ancestor via XPath, and writes to SQLite with an upsert that keeps the richer record:

```sql
ON CONFLICT(id) DO UPDATE SET name=excluded.name,
    profile=COALESCE(NULLIF(excluded.profile,''), profile)
```

That is why labels need step 6 rather than being written directly to CSV — the dedupe has to happen in the database.

## Data Model

| Node | Source | Key properties |
|---|---|---|
| `Artist` | `<artist>` with no `<members>` | `artistId:ID(Artist)`, `name`, `realname`, `profile` |
| `Group` | `<artist>` **with** `<members>` | `groupId:ID(Group)`, `name`, `profile` |
| `Release` | `<release>` reached via a master's `main_release` | `releaseId:ID(Release)`, `year`, `title` |
| `Label` | `<label>`, plus inline refs inside releases | `labelId:ID(Label)`, `name`, `profile` |
| `Master` | `<master>` — **written out only in the superseded importer** | `masterId:ID(Master)`, `year`, `title` |

| Relationship | CSV | Meaning |
|---|---|---|
| `MEMBER_OF` | `artist_group_links.csv` | Artist → Group |
| `CREDITED` | `artist_release_links.csv` | Artist → Release (from `<artists>` and `<extraartists>`) |
| `RELEASED_ON` | `release_label_links.csv` | Release → Label |
| `SUBLABEL` | `label_sublabel_links.csv` | Label → parent Label |

Masters are an intermediate hop, not a node: `process_master` has its `writers["masters"].writerow(...)` call commented out with the note *"less interested in masters, more interested in releases"*. `init_csv_writers` still creates `masters.csv` and `artist_master_links.csv`, so those files get written as headers only. Only the retired `neo4j_bulk_import.py` produces real `Master` nodes.

## Web Interface

`web/app.js` is a single `KnowledgeGraphViewer` class loading graphology, Sigma.js 2.4 and the Neo4j web driver from unpkg. No build step — open it with any static server.

- **Querying.** Input goes through `convertNaturalLanguageToCypher`. A regex fast-path treats anything that looks like a bare name as a substring search and skips the LLM entirely; anything else is sent to Ollama (`llama3.2`) with a prompt containing the schema and few-shot examples, and falls back to a hand-written `CONTAINS` query if Ollama is unreachable.
- **Custom physics.** `updatePhysics` is a hand-rolled force simulation rather than an off-the-shelf layout: mass-scaled inverse-square repulsion, spring attraction along edges toward an ideal distance of 100, a weak pull to the origin, and velocity damping. `handleCollisions` adds O(n²) circle collision with positional separation and elastic impulse response.
- **Degree drives appearance.** Queries return `count(DISTINCT ...) as <key>_degree` alongside the nodes; `renderGraphData` picks up any key ending in `_degree`. Size is logarithmic (`calculateNodeSize`), physics mass is linear (`calculateNodeMass`), so hubs are both bigger and harder to shove.
- **Expansion.** Clicking a node runs `expandNode`, which re-renders additively with `parentNodeId` set so new neighbours spawn in a ring just outside the parent's radius instead of at random coordinates. A `wasDragging` flag stops a drag from registering as a click.
- **Panning is off** (`enableEdgePanning`/`enableNodePanning` false) so dragging always means moving a node.

Node colours in `getNodeColor` cover `Artist` and `Master` plus a set of generic labels (`Person`, `Organization`, `Location`, …) left over from whatever template this started as. `Group`, `Release` and `Label` are **not** in the map and fall through to the default grey — a loose end from the viewer predating those node types.

## Development history

Reconstructed from file mtimes and from what the code itself shows. Mtimes mark the *last* edit, not creation, so a file can be older than its date — `discogs_to_neo4j.py` is stamped 1 Aug but the 14 Jul CLAUDE.md already described it.

**1. First pass — artists and masters only** (`neo4j_bulk_import.sh` 21 Jun, `neo4j_bulk_import.py` 28 Jun). Stdlib `xml.etree`, positional `sys.argv`, hardcoded output filenames, one function per node type. Handles just artists and masters, skipping artist ID 194 (*"Various"*) with the comment *"fucks things up"*. The matching shell script imports `Artist` and `Master` nodes and a single `RELEASED` relationship. Both files are kept for reference and are superseded by `discogs_to_neo4j.py` / `neo4j_admin_import.sh`.

**2. The viewer** (`styles.css` 30 Jun, `app.js` + `index.html` 1 Jul). Built against that first two-label dataset, which is why `getNodeColor` knows only `Artist` and `Master`, why the Ollama prompt says *"contains Artist and Master nodes"*, and why the default query hardcodes `goldie` as a test subject. The physics engine and click-to-expand behaviour all date from here and were never revisited as the schema grew.

**3. Generalising the transform** (by 14 Jul). `discogs_to_neo4j.py` replaces the first pass: `click` CLI, `lxml` for XPath and `getparent()`, all of artists/groups/masters/releases/labels, and per-node-type writer setup. Element lookup went through `fetch_element_ripgrep`, shelling out to `rg -m1 -A200` per release — the strategy the 14 Jul CLAUDE.md documented.

**4. Binary search over the XML** (`xml_binary_search.py` 15 Jul). Ripgrep meant a fresh scan of a multi-GB file per lookup; `fetch_element_ripgrep` still carries the `print(f"ripgrep completed in ...")` timing calls from working that out. `XMLBinarySearcher` exploits the fact that Discogs dumps are **sorted by ID**: it seeks to a byte offset, scans forward to the next element boundary, reads that element's ID, and lazily builds a binary tree of offset→ID as it bisects. It also carries the scars of being hard to get right — visited-node loop detection, a 50-iteration safety cap, a `linear_search` finish once within 10 IDs of the target, and a widened fallback for elements that turn out not to exist. `fetch_element` was switched to it; the ripgrep version is still present but unused.

**5. SQLite index instead** (`releases_to_sqlite.py` + `merge_sqlite_releases.py` 17 Jul, `xml_split.py` 18 Jul). Binary search worked but stayed slow and fragile, so the releases dump got indexed properly instead: one row per release, `id` → raw XML text, batched 1000 at a time. To make that ingest tractable it was parallelised — `xml_split.py` cuts the dump into chunks on `</release>` boundaries and wraps each in synthetic root tags so every chunk parses standalone, and `merge_sqlite_releases.py` recombines the per-chunk DBs (streaming by default, `ATTACH` as an alternative capped at 8 attachments). This is the strategy that stuck: `discogs_to_neo4j.py` prefers `--release-sqlite` and keeps the binary searcher only as the `--release-xml` path.

**6. Index tuning** (`optimise_sqlite_db.py` 22 Jul). A three-line `ANALYZE`, with `VACUUM` commented out — unsurprising on a database this size.

**7. The duplicate hunt** (`dupe_finder.py` + `artist_dupes.txt` 28 Jul). `neo4j-admin` rejects duplicate node IDs, so `dupe_finder.py` scans a node CSV for repeated IDs in column 0. The saved output records **30,655 duplicate artist IDs**, and the shape is diagnostic: every ID appears exactly twice, and the second copies occupy an unbroken run of lines 30656–61310. That is not scattered data corruption, it is the same 30,655-row block written twice — `open_writer` opens CSVs in append mode (`"a"`) and re-writes the header each time, so re-running the transform into an existing output folder duplicates everything already there. The commented-out `writers["labels"].writerow(...)` in `process_label`, tagged *"current theory: sublabels are already included in labels, this is introducing duplicates"*, is the same investigation on the label side.

**8. The real import** (`neo4j_admin_import.sh` 29 Jul). The full schema at last: `Artist`, `Group`, `Label`, `Release` nodes and `MEMBER_OF`, `CREDITED`, `SUBLABEL`, `RELEASED_ON` relationships. Absolute paths under `/Users/henry/src/muggs/map/data/`, mixing `aggregated/` with a dated `2025-07-28/` folder — labels were regenerated separately from the rest.

**9. Fixing labels properly** (`sqlite_to_csv.py` 31 Jul, `discogs_to_neo4j.py` final edits 1 Aug). The label duplicates got a real fix rather than a workaround: route labels through a SQLite table with a merging upsert, then export once with `sqlite_to_csv.py`. This is the most recent work in the project.

The arc, in short: naive streaming parse → generalise → ripgrep random access → hand-rolled binary search → SQLite index with parallel ingest → fight duplicates → dedupe in SQL. The viewer was built early against a two-label graph and has not caught up with the schema.

## Known Issues and Gotchas

- **`open_writer` appends.** CSVs are opened `"a"` with the header re-written each call. Re-running `discogs_to_neo4j.py` into a non-empty output folder silently duplicates rows and re-inserts headers mid-file, which breaks the import. **Always write to a fresh output folder**, or clear it first. This caused the `artist_dupes.txt` incident.
- **`labels.csv` headers are wrong.** Both `init_csv_writers` and `sqlite_to_csv.py` emit `["labelId:ID(Label)", "year", "title"]`, but the labels table columns are `(id, name, profile)`. So `Label.year` holds the name and `Label.title` holds the profile. Fix the header before trusting label properties in Cypher.
- **`neo4j_admin_import.sh` has a typo:** the command ends `--overwrite-destinationr`. It also hardcodes `/Users/henry/...` paths that need editing per machine.
- **`masters.csv` / `artist_master_links.csv` are header-only** with the current transform — see [Data Model](#data-model).
- **Viewer colours are incomplete.** `Group`, `Release` and `Label` render default grey.
- **`expandNode` interpolates `id(center) = ${nodeId}` into Cypher**, and the name fast-path in `convertNaturalLanguageToCypher` interpolates user input into a string literal. Fine for a local single-user tool, not safe to expose.
- **`id()` is deprecated** in current Neo4j in favour of `elementId()`; the viewer relies on `identity` / `id()` throughout.
- **`artist_dupes.txt` is a saved diagnostic** from one 28 Jul run, kept as a record. It is not an input to anything.

## Dependencies and Services

### Python
`lxml`, `click`, `alive_progress`; `sqlite3`, `csv`, `xml.etree` from the stdlib. `ripgrep` (`rg`) on `PATH` only if the unused `fetch_element_ripgrep` is re-enabled. Note the two XML libraries are not interchangeable here — `discogs_to_neo4j.py` and `xml_binary_search.py` need `lxml` for `getparent()` and `xpath()`, while `releases_to_sqlite.py` and the retired `neo4j_bulk_import.py` use stdlib `ElementTree`.

### External services
- **Neo4j** on `bolt://localhost:7687`. Must be **stopped** for `neo4j-admin database import`, which writes into a database named `discogs`.
- **Ollama** on `http://localhost:11434` serving `llama3.2`, for natural-language → Cypher. Optional: the viewer degrades to a substring query if it is unavailable.

### Web (all from unpkg CDN, no build step)
graphology 0.25.4, Sigma.js 2.4.0, neo4j-driver 5.12.0.

### Credentials
`web/config.js` is gitignored; copy `web/config.example.js` and fill in `neo4jPassword`. `app.js` reads `window.MUSIC_GRAPH_CONFIG` and shows an error if no password is set. Do not hardcode credentials back into `app.js` — an earlier revision did, and the password was scrubbed when this repo was first pushed.
