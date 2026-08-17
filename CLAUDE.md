# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A music knowledge graph built from [Discogs monthly data dumps](https://discogs-data-dumps.s3.us-west-2.amazonaws.com/index.html). The Discogs XML dumps are transformed into CSVs for `neo4j-admin database import`, and a small Python server hosts a Sigma.js viewer that searches the graph through a full-text index and explores it by tapping nodes to pull in their neighbours.

The engineering problem is scale: the releases dump is tens of GB of XML in one file, and masters reference releases by ID. The project went through three approaches to that random-access problem before arriving at the current one, which sidesteps it — see [Development history](#development-history).

## Repository Layout

```
.
├── discogs_to_neo4j.py       # the transform: XML dumps -> Neo4j import CSVs
├── sqlite_to_csv.py          # labels staging table -> labels.csv
├── dupe_finder.py            # pre-import check for duplicate node IDs
├── neo4j_admin_import.sh     # the bulk import itself
├── server.py                 # HTTP front end: static viewer + JSON API
├── provision_droplet.sh      # unattended end-to-end run on a fresh droplet
├── cloud-init.example.sh     # paste target for the DO "User data" box
├── fetch_vendor.sh           # download the browser libs into web/vendor/
├── requirements.txt
│
│   # optional: build a reusable random-access index over the releases dump
├── xml_split.py              # split a huge XML dump into well-formed chunks
├── releases_to_sqlite.py     # releases XML -> SQLite (id -> raw XML) index
├── merge_sqlite_releases.py  # merge per-chunk SQLite parts into one DB
├── optimise_sqlite_db.py     # ANALYZE the merged DB
│
└── web/                      # the viewer, served by server.py
    ├── index.html
    ├── app.js                # Viewer: sigma + force layout + API calls
    ├── styles.css
    └── vendor/               # graphology + sigma, fetched by fetch_vendor.sh
```

Data is not in the repo. `.gitignore` excludes `data/`, XML, SQLite and CSV files.

**Requires Python 3.12+** — the transform uses `csv.QUOTE_STRINGS`, added in 3.12. On 3.11 it fails immediately with `AttributeError: module 'csv' has no attribute 'QUOTE_STRINGS'`.

## The Pipeline

There are two routes from dumps to CSVs. **Use the prefilter route** unless you specifically need a reusable release index.

### Prefilter route (default)

```bash
# 1. Transform. Reads the .gz dumps directly -- no decompression, no index.
python discogs_to_neo4j.py \
    --artist-xml  data/discogs_20260801_artists.xml.gz \
    --label-xml   data/discogs_20260801_labels.xml.gz \
    --master-xml  data/discogs_20260801_masters.xml.gz \
    --release-xml data/discogs_20260801_releases.xml.gz \
    --label-sqlite data/labels.db \
    --output-folder data/2026-08-01/

# 2. Flatten the deduplicated labels table, overwriting step 1's placeholder.
python sqlite_to_csv.py --sqlite-path data/labels.db --output-folder data/2026-08-01/

# 3. Sanity-check node CSVs for duplicate IDs.
python dupe_finder.py data/2026-08-01/artists.csv

# 4. Bulk import (Neo4j must be stopped).
./neo4j_admin_import.sh data/2026-08-01 neo4j

# 5. Create the full-text index the viewer searches through.
cypher-shell -d neo4j "CREATE FULLTEXT INDEX entitySearch IF NOT EXISTS
    FOR (n:Artist|Group|Release|Label) ON EACH [n.name, n.title];"

# 6. Serve the viewer.
./fetch_vendor.sh                             # once, populates web/vendor/
NEO4J_PASSWORD=... APP_PASSWORD=... APP_PORT=8000 python server.py
```

`--release-xml` works in two streaming passes. `collect_main_release_ids` reads the masters dump and collects the set of `main_release` IDs; `parse_releases_filtered` then streams the releases dump once, keeping only those. Roughly **3M of the ~18M releases** are ever wanted, so this reads the dump once instead of indexing all of it for random access.

### Release-index route (optional)

Only worth it if you plan repeated experiments against *arbitrary* releases rather than one import, since it builds a queryable index of every release. Steps 1–4 replace step 1 above.

```bash
# 1. Split the releases dump into N chunks, each a well-formed XML document.
python xml_split.py data/discogs_20260801_releases.xml release 16 releases
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

`provision_droplet.sh` does the whole thing unattended on a fresh Ubuntu 24.04 droplet: installs Java 21, Neo4j 5 and the Python deps, downloads and verifies the dumps, runs the prefilter route, imports, and reports node/relationship counts. Every step is idempotent, so it is safe to re-run after a disconnect.

It ends by installing `music-graph.service`, a systemd unit running `server.py`, so the viewer is live at `http://<droplet-ip>/` when the run finishes.

### No-terminal route (works from a phone)

`cloud-init.example.sh` is designed to be pasted into the DigitalOcean control panel's **User data** box when creating the droplet (Advanced Options → Add Initialization scripts). Fill in three values, create the droplet, and it provisions, imports and starts serving on first boot. No SSH, no terminal at any point.

### Terminal route

```bash
export DUMP_DATE=20260801
export NEO4J_PASSWORD='choose-something'
export APP_PASSWORD='choose-something-else'
./provision_droplet.sh
```

### Watching it

Everything goes to disk:

- `/var/log/music-graph.status` — one line, the current stage, or the failing line number and exit code
- `/var/log/music-graph.log` — the full log

Run interactively it tees to the terminal; unattended it redirects straight to the log file, deliberately avoiding `tee` via process substitution, which can drop buffered output when the shell exits and truncate the failure trace.

**Recommended droplet: `s-4vcpu-8gb`** (8 GB / 4 vCPU / 160 GB SSD). Expect ~4–6 hours. DigitalOcean bills hourly and inbound bandwidth is free, so a one-off run destroyed afterwards costs well under a pound.

Peak disk on the prefilter route, against the real 2026-08 dump sizes:

| | |
|---|---|
| `.gz` dumps (releases alone is 10.4 GB) | 11.5 GB |
| `labels.db` staging | ~1 GB |
| generated CSVs | ~4 GB |
| Neo4j store | ~15 GB |
| **peak** | **~32 GB** |

That fits the built-in 160 GB SSD with room to spare, so no block-storage volume is needed. The index route would instead need roughly 2× the *uncompressed* releases dump — about 220 GB — and therefore a paid volume.

Going smaller than 8 GB is a false economy: `neo4j-admin import` gets tight on 4 GB and the saving over a single run is trivial next to a failed multi-hour job. The script adds a 4 GB swapfile on boxes under 16 GB as insurance.

Two deployment notes baked into the script:

- **It imports into the database named `neo4j`**, not `discogs`. Community Edition serves exactly one user database; importing into another name yields a database Neo4j will not start.
- **Only the viewer port is exposed.** Neo4j stays on localhost and `ufw` allows just 22 and `APP_PORT`, so 7474/7687 are unreachable from outside even if the Neo4j config changes. The viewer is plain HTTP, so its basic-auth password crosses the wire in the clear — use a throwaway, and do not reuse the database password. `APP_PASSWORD` defaults to `NEO4J_PASSWORD` only so a run is not blocked on setting it.
- **Credentials live in `/etc/music-graph.env`, mode 600**, not in the systemd unit, which is world-readable by default. The service runs as the unprivileged `neo4j` user with `AmbientCapabilities=CAP_NET_BIND_SERVICE` so it can bind port 80 without root.

## Web Interface

Rebuilt from scratch. The original viewer opened a bolt connection **from the browser** with the password inlined in a JS file, asked Ollama to write Cypher, and interpolated user input into query strings — none of which survives being served on a public port.

### `server.py` — the front end

A single-file HTTP server: static files from `web/`, plus a three-endpoint JSON API. All Cypher lives here, is parameterised, and only reads. The browser never sees database credentials and cannot submit a query of its own.

| Endpoint | Returns |
|---|---|
| `GET /api/stats` | per-label totals |
| `GET /api/search?q=&limit=` | seed nodes matching the term, with their immediate edges |
| `GET /api/expand?id=&limit=` | neighbours of one node, by `elementId` |

- **Search goes through a full-text index** (`entitySearch`). A `CONTAINS` scan over ~15M nodes would never return in usable time. The search term is the only user text that reaches the database as anything but a bound parameter, so `clean_term` strips Lucene operators before it gets there.
- **`stats` counts one label at a time** (`MATCH (n:Artist) RETURN count(n)`), which hits Neo4j's count store and is O(1) rather than scanning.
- **HTTP basic auth** guards everything, compared with `hmac.compare_digest`. Set `APP_ALLOW_ANONYMOUS=1` to disable it for local use.
- **Limits are clamped** to `MAX_LIMIT` (300) server-side, and profile text is truncated to 600 characters — Discogs profiles run to thousands of words.

### `web/app.js` — the viewer

- **Layout is Fruchterman–Reingold with a cooling schedule**, run in animation-frame batches. The view is capped at a few hundred nodes so O(n²) repulsion is free and Barnes–Hut is not worth a dependency. Cooling is what the original lacked: it ran a constant-energy simulation forever with elastic collision impulses fighting the springs, so hub-heavy subgraphs jittered indefinitely instead of settling.
- **Colour by node type**, all four of them, with a legend: Artist blue, Group violet, Release rose, Label green. Sigma renders a node's `label` attribute, so the *type* is stored separately as `kind` — writing the title into `label` is what the display needs, and reading the type back out of it is a mistake worth not repeating.
- **Size by degree within the visible subgraph**, logarithmic. Using true global degree would let a hub like "Various" swamp everything.
- **Tap to select** (opens the detail panel), **tap Expand** to pull in neighbours, which merge into the existing graph rather than replacing it. Already-expanded nodes are tracked so the button disappears.
- **Built for a phone**: 390px-wide layout tested in Chromium, 16px inputs so iOS does not zoom on focus, `touch-action: none` so sigma owns pan/zoom gestures, and the panel docks to the bottom under 560px.

### Vendored libraries

`graphology` and `sigma` are downloaded into `web/vendor/` by `fetch_vendor.sh` rather than loaded from a CDN, so the page works on flaky mobile data. `web/vendor/` is gitignored; provisioning runs the script automatically.

Note that `graphology-layout-forceatlas2` **has no UMD build** in its npm package, so the obvious unpkg path for it 404s — that is why the layout is implemented directly rather than pulled in.

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
- **The full-text index must exist** before search works. `provision_droplet.sh` creates it and waits via `db.awaitIndexes`; building it over ~15M nodes takes a while. Without it, `/api/search` returns a "query failed" error.
- **Dump URL pattern is unverified** from the sandbox this was written in (egress policy blocked the bucket). `provision_droplet.sh` fails loudly with the index URL if a download 404s.
- **The viewer is plain HTTP.** Fine for a throwaway droplet; put it behind a TLS terminator before treating it as anything more.

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
`pip install -r requirements.txt` — `lxml`, `click`, `alive_progress` for the pipeline, `neo4j` for `server.py`. The two XML libraries are not interchangeable: `discogs_to_neo4j.py` needs `lxml` for `getparent()`/`xpath()`, while `releases_to_sqlite.py` uses stdlib `ElementTree`.

### External services
- **Neo4j 5** (needs Java 17 or 21) on `bolt://localhost:7687`. Must be **stopped** for `neo4j-admin database import`.

Ollama is no longer used. The rebuilt viewer searches a full-text index instead of asking an LLM to write Cypher.

### Web (vendored, no build step)
graphology 0.25.4 and Sigma.js 2.4.0 in `web/vendor/`, fetched by `fetch_vendor.sh`. No CDN at runtime, no browser-side database driver.

### Credentials
`server.py` reads `NEO4J_PASSWORD` and `APP_PASSWORD` from the environment; provisioning puts them in `/etc/music-graph.env` at mode 600. Nothing credential-bearing is served to the browser. Do not reintroduce credentials into `web/` — the original `app.js` hardcoded the Neo4j password, and it had to be scrubbed when this repo was first pushed.
