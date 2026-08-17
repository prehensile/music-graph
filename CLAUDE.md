# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A music knowledge graph built from [Discogs monthly data dumps](https://discogs-data-dumps.s3.us-west-2.amazonaws.com/index.html). The Discogs XML dumps are transformed into CSVs for `neo4j-admin database import`, and a browser-based Sigma.js viewer explores the resulting graph with click-to-expand navigation and natural-language querying via a local Ollama model.

The engineering problem is scale: the releases dump is tens of GB of XML in one file, and masters reference releases by ID. The project went through three approaches to that random-access problem before arriving at the current one, which sidesteps it — see [Development history](#development-history).

## Repository Layout

```
.
├── discogs_to_neo4j.py       # the transform: XML dumps -> Neo4j import CSVs
├── sqlite_to_csv.py          # labels staging table -> labels.csv
├── dupe_finder.py            # pre-import check for duplicate node IDs
├── neo4j_admin_import.sh     # the bulk import itself
├── provision_droplet.sh      # unattended end-to-end run on a fresh droplet
├── requirements.txt
│
│   # optional: build a reusable random-access index over the releases dump
├── xml_split.py              # split a huge XML dump into well-formed chunks
├── releases_to_sqlite.py     # releases XML -> SQLite (id -> raw XML) index
├── merge_sqlite_releases.py  # merge per-chunk SQLite parts into one DB
├── optimise_sqlite_db.py     # ANALYZE the merged DB
│
└── web/                      # Sigma.js graph viewer
    ├── index.html
    ├── app.js                # KnowledgeGraphViewer: sigma + physics + Ollama
    ├── styles.css
    └── config.example.js     # copy to config.js and add credentials
```

Data is not in the repo. `.gitignore` excludes `data/`, XML, SQLite and CSV files.

**Requires Python 3.12+** — the transform uses `csv.QUOTE_STRINGS`, added in 3.12. On 3.11 it fails immediately with `AttributeError: module 'csv' has no attribute 'QUOTE_STRINGS'`.

## The Pipeline

There are two routes from dumps to CSVs. **Use the prefilter route** unless you specifically need a reusable release index.

### Prefilter route (default)

```bash
# 1. Transform. Reads the .gz dumps directly -- no decompression, no index.
python discogs_to_neo4j.py \
    --artist-xml  data/discogs_20250801_artists.xml.gz \
    --label-xml   data/discogs_20250801_labels.xml.gz \
    --master-xml  data/discogs_20250801_masters.xml.gz \
    --release-xml data/discogs_20250801_releases.xml.gz \
    --label-sqlite data/labels.db \
    --output-folder data/2025-08-01/

# 2. Flatten the deduplicated labels table, overwriting step 1's placeholder.
python sqlite_to_csv.py --sqlite-path data/labels.db --output-folder data/2025-08-01/

# 3. Sanity-check node CSVs for duplicate IDs.
python dupe_finder.py data/2025-08-01/artists.csv

# 4. Bulk import (Neo4j must be stopped).
./neo4j_admin_import.sh data/2025-08-01 neo4j

# 5. Serve the viewer.
cp web/config.example.js web/config.js   # then fill in credentials
cd web && python -m http.server 8000
```

`--release-xml` works in two streaming passes. `collect_main_release_ids` reads the masters dump and collects the set of `main_release` IDs; `parse_releases_filtered` then streams the releases dump once, keeping only those. Roughly **3M of the ~18M releases** are ever wanted, so this reads the dump once instead of indexing all of it for random access.

### Release-index route (optional)

Only worth it if you plan repeated experiments against *arbitrary* releases rather than one import, since it builds a queryable index of every release. Steps 1–4 replace step 1 above.

```bash
# 1. Split the releases dump into N chunks, each a well-formed XML document.
python xml_split.py data/discogs_20250801_releases.xml release 16 releases
# 2. Index each chunk into its own SQLite DB (parallelise these).
python releases_to_sqlite.py --xml-file data/...releases.xml.1 --db-path data/releases_part1.db
# 3. Merge the per-chunk DBs.
python merge_sqlite_releases.py --pattern "data/releases_part*.db" --output data/releases.db
# 4. Build query statistics.
python optimise_sqlite_db.py data/releases.db
# then the transform, with --release-sqlite in place of --release-xml
```

This route needs the dump uncompressed on disk, and both `xml_split` and `merge_sqlite_releases` duplicate their input, so peak disk is roughly **2× the uncompressed dump** — around 230 GB, versus about 35 GB for the prefilter route.

It is also **less robust**: it calls `process_master` per master and writes a release row for each, so two masters sharing a `main_release` emit a duplicate `Release` ID and the import fails. The prefilter route collects IDs into a set, so duplicates collapse for free.

## Why both SQLite and CSV

These are not competing formats and neither replaced the other.

**CSV is the required output.** `neo4j-admin database import` reads CSV and nothing else, so the pipeline must terminate in CSVs with `:ID`/`:START_ID`/`:END_ID` typed headers. That has never changed.

**SQLite is a staging area in the middle**, for one specific job in the default route: deduplicating labels. Labels arrive from two sources — the labels dump, and inline `<label>` refs inside release listings — and CSV cannot revise a row you have already written. SQLite can:

```sql
ON CONFLICT(id) DO UPDATE SET name=excluded.name,
    profile=COALESCE(NULLIF(excluded.profile,''), profile)
```

That upsert keeps whichever record is richer, which is why labels need the separate `sqlite_to_csv.py` export step instead of being written straight out.

The optional release index is the *other*, unrelated use of SQLite — a read-only lookup table, `(id INTEGER PRIMARY KEY, content TEXT)` holding raw XML text. The asymmetry in the CLI is the quickest way to keep them straight:

| | `--release-sqlite` | `--label-sqlite` |
|---|---|---|
| Role | read-only input index | write-side staging table |
| Built by | `releases_to_sqlite.py` | `discogs_to_neo4j.py` itself |
| `click.Path` | `exists=True` — must pre-exist | plain — created if absent |
| Needed in default route? | no | yes |

Per entity in the default route: **Artists/Groups** XML → CSV directly; **Releases** streamed and filtered → CSV; **Labels** SQLite accumulate → CSV; **import** CSV only, always.

## Data Model

| Node | Source | Key properties |
|---|---|---|
| `Artist` | `<artist>` with no `<members>` | `artistId:ID(Artist)`, `name`, `realname`, `profile` |
| `Group` | `<artist>` **with** `<members>` | `groupId:ID(Group)`, `name`, `profile` |
| `Release` | `<release>` named by a master's `main_release` | `releaseId:ID(Release)`, `year`, `title` |
| `Label` | `<label>`, plus inline refs inside releases | `labelId:ID(Label)`, `name`, `profile` |

| Relationship | CSV | Direction |
|---|---|---|
| `MEMBER_OF` | `artist_group_links.csv` | Artist → Group |
| `CREDITED` | `artist_release_links.csv` | Artist → Release (`<artists>` and `<extraartists>`) |
| `RELEASED_ON` | `release_label_links.csv` | Release → Label |
| `SUBLABEL` | `label_sublabel_links.csv` | sublabel Label → parent Label |

`Artist` and `Group` are separate Neo4j ID spaces drawn from the same Discogs ID sequence — a given ID lands in exactly one of `artists.csv` or `groups.csv` depending on whether it has `<members>`. Relationship rows must point into the right space; getting it backwards produces dangling edges the importer rejects rather than a silently wrong graph.

**Masters are an intermediate hop, not a node.** There is no `Master` label in the graph.

## Running it on a droplet

`provision_droplet.sh` does the whole thing unattended on a fresh Ubuntu 24.04 droplet: installs Java 21, Neo4j 5 and the Python deps, downloads and verifies the dumps, runs the prefilter route, imports, and prints node/relationship counts. Every step is idempotent, so it is safe to re-run after a disconnect.

```bash
export DUMP_DATE=20250801
export NEO4J_PASSWORD='choose-something'
nohup ./provision_droplet.sh > /var/log/music-graph.log 2>&1 &
```

**Recommended droplet: `s-4vcpu-8gb`** (8 GB / 4 vCPU / 160 GB SSD). Peak disk on the prefilter route is ~35 GB — 10 GB of `.gz` dumps, ~5 GB of CSVs, ~15 GB of Neo4j store — so no block-storage volume is needed. Expect ~4–6 hours. DigitalOcean bills hourly and inbound bandwidth is free, so a one-off run that is destroyed afterward costs well under a pound. The index route would need a ~300 GB volume instead.

Going smaller than 8 GB is a false economy: `neo4j-admin import` gets tight on 4 GB and the saving over a single run is trivial next to a failed multi-hour job. The script adds a 4 GB swapfile on boxes under 16 GB as insurance.

Two deployment notes baked into the script:

- **It imports into the database named `neo4j`**, not `discogs`. Community Edition serves exactly one user database; importing into another name yields a database Neo4j will not start. It is also what the viewer's driver picks up by default.
- **Bolt stays bound to localhost.** Do not open 7687/7474 to the internet. Reach it over an SSH tunnel: `ssh -N -L 7687:localhost:7687 -L 7474:localhost:7474 root@<ip>`.

## Web Interface

`web/app.js` is a single `KnowledgeGraphViewer` class loading graphology, Sigma.js 2.4 and the Neo4j web driver from unpkg. No build step.

- **Querying.** `convertNaturalLanguageToCypher` has a regex fast-path that treats anything resembling a bare name as a substring search and skips the LLM; anything else goes to Ollama (`llama3.2`) with a schema prompt and few-shot examples, falling back to a hand-written `CONTAINS` query if Ollama is unreachable.
- **Custom physics.** `updatePhysics` is hand-rolled rather than an off-the-shelf layout: mass-scaled inverse-square repulsion, spring attraction toward an ideal edge length of 100, a weak pull to the origin, velocity damping. `handleCollisions` adds O(n²) circle collision with positional separation and elastic impulse response.
- **Degree drives appearance.** Queries return `count(DISTINCT ...) as <key>_degree`; `renderGraphData` picks up any key ending in `_degree`. Size is logarithmic, physics mass linear, so hubs are both bigger and harder to shove.
- **Expansion.** Clicking a node runs `expandNode`, re-rendering additively with `parentNodeId` set so new neighbours spawn in a ring just outside the parent's radius. A `wasDragging` flag stops a drag registering as a click.
- **Panning is off**, so dragging always means moving a node.

The viewer was written against an early two-label graph and has **not** caught up with the schema. `getNodeColor` covers `Artist` and `Master` plus generic leftovers (`Person`, `Organization`, …), so `Group`, `Release` and `Label` render default grey. The Ollama prompt still describes the graph as containing "Artist and Master nodes", and the default query hardcodes `goldie`. `Master` no longer exists as a node at all.

## Development history

Reconstructed from file dates in the original archives and from the code itself. Superseded files are recoverable from commit `bdd80c1`, which imported the project verbatim before any cleanup.

**1. First pass — artists and masters only** (21–28 Jun 2025, *removed*). Stdlib `xml.etree`, positional `sys.argv`, hardcoded filenames, skipping artist ID 194 (*"Various"*) with the comment *"fucks things up"*.

**2. The viewer** (30 Jun – 1 Jul). Built against that two-label dataset, which explains every schema anachronism above.

**3. Generalising the transform** (by 14 Jul). `click` CLI, `lxml` for XPath and `getparent()`, all node types. Element lookup shelled out to `rg -m1 -A200` per release.

**4. Binary search over the XML** (15 Jul, *removed*). Ripgrep meant rescanning a multi-GB file per lookup. `XMLBinarySearcher` exploited the dumps being **sorted by ID**: seek to an offset, scan to the next element boundary, read its ID, lazily building a binary tree of offset→ID while bisecting. Loop detection, a 50-iteration cap and a linear-search finish are the scars of getting it right.

**5. SQLite index instead** (17–18 Jul). Binary search worked but stayed slow and fragile, so the dump was indexed properly, parallelised via `xml_split.py` and recombined by `merge_sqlite_releases.py`.

**6. Index tuning** (22 Jul). `ANALYZE`, with `VACUUM` commented out.

**7. The duplicate hunt** (28 Jul). `dupe_finder.py`, written because `neo4j-admin` rejects duplicate node IDs. Its saved output recorded 30,655 duplicate artist IDs, each appearing exactly twice with the second copies in an unbroken run of lines 30656–61310 — the same block written twice, because `open_writer` appends.

**8. The real import** (29 Jul). Full schema: `Artist`, `Group`, `Label`, `Release` and the four relationships.

**9. Fixing labels properly** (31 Jul – 1 Aug). Route labels through SQLite with a merging upsert, then export once.

**10. The prefilter** (current). The realisation that only a master's `main_release` is ever imported — ~3M of ~18M releases — so the whole random-access problem that drove steps 3–5 can be avoided by collecting the wanted IDs first and streaming the dump once, straight from `.gz`. Cuts peak disk from ~230 GB to ~35 GB and runtime from days to hours.

The arc: naive parse → generalise → ripgrep → binary search → SQLite index → *stop needing random access at all*. Throughout, CSV remained the output format.

## Known Issues and Gotchas

- **`open_writer` appends.** CSVs open in `"a"` mode with the header re-written per call, so re-running into a folder holding output duplicates every row and injects headers mid-file. `discogs_to_neo4j.py` **refuses to start** if the output folder contains any `.csv`. The append mode itself is unchanged — switching to `"w"` would clobber output on partial re-runs, so the guard is deliberately a refusal.
- **`labels.csv` is written twice.** The transform creates a header-only placeholder whenever `--label-xml` is given; `sqlite_to_csv.py` overwrites it with the real export. Skipping that second step leaves an empty label set and dangling `RELEASED_ON` edges.
- **Sublabels can dangle.** Sublabels go into `label_sublabel_links.csv` but not `labels.csv`, assuming every sublabel also appears as a top-level `<label>`. True for full dumps, not partial ones; add `--skip-bad-relationships=true` if the import aborts on these.
- **`expandNode` interpolates `id(center) = ${nodeId}` into Cypher**, and the name fast-path interpolates user input into a string literal. Fine for a local single-user tool, not safe to expose.
- **`id()` is deprecated** in current Neo4j in favour of `elementId()`; the viewer relies on `identity`/`id()` throughout.
- **Viewer colours are incomplete** — see Web Interface.
- **Dump URL pattern is unverified** from the sandbox this was written in (egress policy blocked the bucket). `provision_droplet.sh` fails loudly with the index URL if a download 404s.

### Fixed along the way

Non-obvious symptoms, recorded in case you hit them in an older checkout:

- **`MEMBER_OF` edges were inverted.** `process_artist` wrote `[group_id, member_id]` under a `:START_ID(Artist),:END_ID(Group)` header. Since a group's ID lives in `groups.csv` and a member's in `artists.csv`, *both* columns pointed at the wrong ID space and every row was dangling in both directions — all rejected.
- **`xml_split.py` silently truncated the last chunk.** Its `if tag_end is None: break` exited without extending coverage to EOF, so the trailing bytes — including the root's closing tag — were dropped. As the final chunk is also the one given no synthetic tail, it came out unterminated and *every record in it* was lost when `releases_to_sqlite.py` hit the parse error. Coverage to `xml_size` is now asserted after the loop regardless of which branch exits it.
- **`xml_split.py` scanned one byte at a time.** `fp.read(1)` in a loop managed ~2.7 MiB/s — about 12 hours on a 110 GiB dump for that step alone. Now reads 1 MiB blocks with an overlap so boundary-straddling matches are not missed, verified byte-identical to the old scanner across 308 start positions.
- **`labels.csv` headers were wrong.** Both writers emitted `["labelId:ID(Label)", "year", "title"]` over columns `(id, name, profile)`, so `Label.year` held the name and `Label.title` the profile.
- **`neo4j_admin_import.sh` ended in `--overwrite-destinationr`** and hardcoded `/Users/henry/...` paths. Now takes the CSV folder and database name as arguments and preflights every expected CSV.
- **Cleared elements accumulated under the root.** The parse loop called `element.clear()` but never pruned the emptied siblings, costing over a gigabyte across a full releases dump. `stream_elements` now deletes processed previous siblings, keeping peak memory flat.
- **Progress bookkeeping ran per XML event**, including every nested element — over a billion `tell()`+`bar()` calls on a releases dump, measurably hours of pure overhead. Now updated per matched element via an iterparse `tag=` filter.
- **Header-only `masters.csv`/`artist_master_links.csv`** were created every run and never written. Those writers are gone.

## Dependencies and Services

### Python 3.12+
`pip install -r requirements.txt` — `lxml`, `click`, `alive_progress`. The two XML libraries are not interchangeable: `discogs_to_neo4j.py` needs `lxml` for `getparent()`/`xpath()`, while `releases_to_sqlite.py` uses stdlib `ElementTree`.

### External services
- **Neo4j 5** (needs Java 17 or 21) on `bolt://localhost:7687`. Must be **stopped** for `neo4j-admin database import`.
- **Ollama** on `http://localhost:11434` serving `llama3.2`, for natural-language → Cypher. Optional; the viewer degrades to a substring query without it.

### Web (unpkg CDN, no build step)
graphology 0.25.4, Sigma.js 2.4.0, neo4j-driver 5.12.0.

### Credentials
`web/config.js` is gitignored; copy `web/config.example.js` and fill in `neo4jPassword`. `app.js` reads `window.MUSIC_GRAPH_CONFIG` and errors if no password is set. Do not hardcode credentials back into `app.js` — an earlier revision did, and the password was scrubbed when this repo was first pushed.
