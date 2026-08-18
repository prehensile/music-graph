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
├── graph_stats.py            # what the build produced, and what it cost
├── progress.py               # periodic plain-text progress lines
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
├── notes/                    # session handovers; read the newest first
└── web/                      # the viewer, served by server.py
    ├── index.html
    ├── app.js                # Viewer: sigma + force layout + API calls
    ├── logs.html             # build-progress page, served at /logs
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
| `Artist` | `<artist>` with no `<members>` | `artistId:ID(Entity)`, `name`, `realname`, `profile` |
| `Group` | `<artist>` **with** `<members>` | `groupId:ID(Entity)`, `name`, `profile` |
| `Release` | `<release>` named by a master's `main_release` | `releaseId:ID(Release)`, `year`, `title` |
| `Label` | `<label>`, plus inline refs inside releases | `labelId:ID(Label)`, `name`, `profile` |

| Relationship | CSV | Direction |
|---|---|---|
| `MEMBER_OF` | `artist_group_links.csv` | Artist → Group (both `Entity`) |
| `CREDITED` | `artist_release_links.csv` | Artist **or Group** → Release (`<artists>` and `<extraartists>`) |
| `RELEASED_ON` | `release_label_links.csv` | Release → Label |
| `SUBLABEL` | `label_sublabel_links.csv` | sublabel Label → parent Label |

`Artist` and `Group` are separate Neo4j **labels** but share one import **ID space**, `Entity`. They have to. Discogs issues artist and group IDs from a single sequence, and a release credits a band with exactly the same kind of ID it uses for a person — `process_release` cannot tell which it has. With separate spaces, every release credited to a band emitted a dangling `CREDITED` row, because the band's ID lives in `groups.csv` while the edge declared `:START_ID(Artist)`. A shared space stays unique because each Discogs ID lands in exactly one of the two files.

That makes the ID space the thing to get right, and the importer is the check on it: dangling edges are rejected rather than producing a silently wrong graph.

**Masters are an intermediate hop, not a node.** There is no `Master` label in the graph.

**Two Discogs ids are placeholders, not artists, and are dropped from `CREDITED` entirely.** `194` is "Various" (a compilation credited as a whole) and `355` is "Unknown Artist". Neither has ever had its own artist page — confirmed absent from the artists dump, not merely dropped by a parsing bug — so every release crediting them would otherwise be a dangling edge. `PLACEHOLDER_ARTIST_IDS` in `discogs_to_neo4j.py` filters them out in `process_release` before the row is ever written. Writing a generic node for them instead would be worse than dropping the edge: it would link together thousands of unrelated releases that share nothing but an unknown or various performer.

## Running it on a droplet

`provision_droplet.sh` does the whole thing unattended on a fresh Ubuntu 24.04 droplet: installs Java 21, Neo4j 5 and the Python deps, downloads and verifies the dumps, runs the prefilter route, imports, and reports node/relationship counts. Every step is idempotent, so it is safe to re-run after a disconnect.

It ends by installing `music-graph.service`, a systemd unit running `server.py`, so the viewer is live at `http://<droplet-ip>/` when the run finishes.

### No-terminal route (works from a phone)

`cloud-init.example.sh` is designed to be pasted into the DigitalOcean control panel's **User data** box when creating the droplet (Advanced Options → Add Initialization scripts). Fill in three values, create the droplet, and it provisions, imports and starts serving on first boot. No SSH, no terminal at any point.

### Cloning a private repo

The droplet clones this repo over unauthenticated HTTPS, which only works if it is public. If it is private, set `GITHUB_TOKEN` to a fine-grained token with read-only **Contents** access to just this repo.

`fetch_repo` passes it per git command via `http.extraheader` rather than embedding it in the remote URL, so it never lands in `.git/config`. It is still visible in the process list while git runs, and DigitalOcean serves user-data to anything running on the droplet, so scope the token to one repo, give it a short expiry, and revoke it when the build finishes. A failed clone says which of the two situations you are in.

### Terminal route

```bash
export DUMP_DATE=20260801
export NEO4J_PASSWORD='choose-something'
export APP_PASSWORD='choose-something-else'
./provision_droplet.sh
```

`START_AT=<n>` resumes from a given step (1-based, against the list in `STEP_NAMES`), for when the earlier work is already done — `START_AT=9` goes straight to the import. Preflight also skips the URL check for any dump already verified on disk, and treats 429/5xx as transient, so a rate limit cannot fail a run that needs no downloads.

### Watching it

**The viewer serves a progress page at `/logs`.** The service is deliberately started *before* the slow steps, so the whole run can be watched from a browser: a step checklist with timings, a progress bar, and a live tail of the log, polled every 4 seconds. The graph endpoints stay broken until the import lands, and the page and the viewer both say so rather than failing silently.

Everything is on disk too:

| | |
|---|---|
| `/var/log/music-graph.status` | one line — current stage, or the failing line number and exit code |
| `/var/log/music-graph.steps.jsonl` | one JSON object per step transition (`pending`/`running`/`done`/`failed`) |
| `/var/log/music-graph.log` | the full log |
| `/var/log/music-graph.report.txt` | the build report, written at the end by `graph_stats.py` |

The steps file is append-only, one record per transition, with later records superseding earlier ones for the same step number. A flat file needs no coordination between the shell script and the web server, and survives either being restarted. `main()` seeds a `pending` record for every step up front so the page shows the full checklist by name from the start rather than growing as the run proceeds.

Run interactively the script tees to the terminal; unattended it redirects straight to the log file, deliberately avoiding `tee` via process substitution, which can drop buffered output when the shell exits and truncate the failure trace.

### Build statistics

`graph_stats.py` reports what a build produced and what it cost: node and relationship counts, derived ratios (credited artists per release, members per group), per-step timings pulled from `music-graph.steps.jsonl`, and the sizes of the dumps, CSVs and Neo4j store. Provisioning runs it at the end and saves the output; run it again any time.

```bash
python graph_stats.py            # fast summary, about a second
python graph_stats.py --deep     # plus hubs, unconnected nodes, releases by decade
python graph_stats.py --json -o stats.json
```

The default report is cheap because every count comes from Neo4j's count store — `MATCH (n:Artist) RETURN count(n)` is O(1), not a scan of 10M nodes. `--deep` is where the scans live: degree ordering, `WHERE NOT (n)--()`, and grouping releases by decade all walk their label. Each deep query is timed and individually fault-tolerant, so a slow or failing one costs only its own section rather than the whole report — worth having on a box where an overloaded Neo4j may time a query out.

The call from `provision_droplet.sh` is deliberately tolerant (`|| echo ...`): by the time it runs, hours of downloading and transforming are already banked, and a stats failure must not fail the provision.

### Progress reporting in the Python scripts

`progress.py` prints a plain line every `PROGRESS_INTERVAL` seconds (default 30, `0` to silence). This replaced `alive_progress`, which was a poor fit here on three counts: it redraws with control characters aimed at a terminal, it hooks `sys.stdout` so anything printed inside its context gets rewritten with an `on <n>:` prefix, and it costs per record — roughly 1.2 µs of formatting plus a bar call, about 40 seconds across a full releases dump. A timestamped line behaves the same whether or not anyone is watching, greps cleanly, and reads well in the `/logs` pane.

```
  [release]  63.1% | 11,383,204 releases | 18,941/s | ETA 21m 14s
```

`Heartbeat` measures progress in whatever unit you give it — bytes consumed when streaming a file, rows when draining a query — so percentages stay accurate regardless of record count.

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
| `GET /api/progress` | provisioning steps, status and log tail — reads files only, never the database |

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
- **Sublabels are registered as `Label` nodes.** They used to go into `label_sublabel_links.csv` only, on the theory that every sublabel also appears as a top-level `<label>`; where that does not hold the `SUBLABEL` edge dangles. `process_label` now upserts the sublabel too, which is safe because a later top-level record supersedes the name-only one.
- **`CREDITED` and `MEMBER_OF` still carry some genuine referential drift** — releases and groups crediting artist ids that never got their own artist page, distinct from the `194`/`355` placeholders (see Data Model), which are filtered out before the row is ever written. Measured on the 2026-08 dump: on the order of ten thousand rows, well under 0.1% of either file. `neo4j_admin_import.sh` therefore runs with `--skip-bad-relationships=true` and `--bad-tolerance=50000` — well below the smallest true relationship file (~300k rows) — so this residual drift passes while a systemic fault, such as edges written into the wrong ID space (millions of rows, not thousands), still aborts the import. Anything skipped is listed in `import.report` and summarised at the end of the step.
- **`fetch_repo` can rewrite the running script.** `git reset --hard` replaces `provision_droplet.sh` while bash is part-way through executing it, and bash reads a script incrementally by byte offset, so the remainder of the run can be garbage. The step hashes itself before and after the checkout and re-execs once (guarded by `MUSIC_GRAPH_REEXEC`) if it changed. Re-running is cheap because every step is idempotent.
- **`neo4j-admin` writes `import.report` relative to the current directory.** Provisioning runs from `/` (cloud-init's cwd, which `sudo -u` does not change), so the unprivileged `neo4j` user cannot create it and the import dies with `AccessDeniedException: /import.report` *after* the transform has already run. The path is now passed explicitly with `--report-file`.
- **The full-text index must exist** before search works. `provision_droplet.sh` creates it and waits via `db.awaitIndexes`; building it over ~15M nodes takes a while. Without it, `/api/search` returns a "query failed" error.
- **Dumps download from `data.discogs.com`, not the S3 bucket directly.** Anonymous GETs against `discogs-data-dumps.s3.us-west-2.amazonaws.com` return 403, and since the bucket also denies anonymous listing, S3 answers 403 rather than 404 for a key that is simply absent — so a wrong path looks like a permissions problem. The front end takes the object key url-encoded in a query parameter: `https://data.discogs.com/?download=data%2F2026%2Fdiscogs_20260801_artists.xml.gz`. `DUMP_HOST` and `DUMP_PREFIX` override it if that ever changes.
- **Dumps are fetched with `wget`, not `curl`, and that is deliberate.** curl's own `--retry` restarts the transfer from byte 0 — every retry goes out with no `Range` header, measured directly — so an interrupted 10 GB download is re-fetched from scratch, repeatedly. `wget --continue --tries` resumes instead, reissuing with the correct `Range`; against a server severing the connection mid-body the offsets advance `0 → 3285 → 5475 → 6935 → 7908` inside a single invocation. wget 1.x also speaks only HTTP/1.1, so the Cloudflare HTTP/2 stream reset (`curl: (92) INTERNAL_ERROR`) that killed the first real run cannot recur. `--read-timeout` abandons a connection stalled mid-body, which is how that failure presented.
- **`fetch_dump` verifies inside its retry loop, not after it.** Both wget and curl answer a 416 response to a resume request by reporting *success* — 416 is taken to mean the local file is already complete — so a truncated partial otherwise looks like a clean download. Only the checksum distinguishes them, which is why each attempt is verified before the function returns. A verified file gets a `.ok` sentinel so re-runs skip it without re-hashing 10 GB.
- **Preflight HEADs all four dump URLs** so a wrong date or path fails in seconds instead of several minutes in. It must be HEAD: a server that ignores `Range` answers a ranged GET with the whole body, which would mean pulling 10 GB during a two-second check.
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
`pip install -r requirements.txt` — `lxml` and `click` for the pipeline, `neo4j` for `server.py`. Progress reporting is `progress.py`, no dependency. The two XML libraries are not interchangeable: `discogs_to_neo4j.py` needs `lxml` for `getparent()`/`xpath()`, while `releases_to_sqlite.py` uses stdlib `ElementTree`.

### External services
- **Neo4j 5** (needs Java 17 or 21) on `bolt://localhost:7687`. Must be **stopped** for `neo4j-admin database import`.

Ollama is no longer used. The rebuilt viewer searches a full-text index instead of asking an LLM to write Cypher.

### Web (vendored, no build step)
graphology 0.25.4 and Sigma.js 2.4.0 in `web/vendor/`, fetched by `fetch_vendor.sh`. No CDN at runtime, no browser-side database driver.

### Credentials
`server.py` reads `NEO4J_PASSWORD` and `APP_PASSWORD` from the environment; provisioning puts them in `/etc/music-graph.env` at mode 600. Nothing credential-bearing is served to the browser. Do not reintroduce credentials into `web/` — the original `app.js` hardcoded the Neo4j password, and it had to be scrubbed when this repo was first pushed.
