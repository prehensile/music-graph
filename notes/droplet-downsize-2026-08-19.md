# Droplet downsize: 8GB → 4GB, 2026-08-19

Written mid-migration, for whoever (human or agent) picks this up next.
Current droplet: `s-4vcpu-8gb`, region `ams3`, IP `161.35.92.143`, Ubuntu
24.04. Sized for the import; steady-state footprint is much smaller than
that box — see the sizing rationale below before assuming bigger is safer.

## Why

Measured on the live droplet after the import had been running a while:

| | |
|---|---|
| Neo4j JVM RSS | 1.46 GB (no `dbms.memory.*` override — JVM defaults) |
| Neo4j store on disk | 3.1 GB — 15,163,917 nodes, 19,415,901 relationships |
| `server.py` RSS | ~68 MB |
| Load average | 0.05 — a single-user read-only demo, not CPU-bound |
| Disk used | 25 GB of 154 GB; 13 GB of that is `/var/lib/music-graph/data` (raw dumps, `labels.db`, CSVs), none of which serving needs |

Target: `s-2vcpu-4gb`, same region. 4 GB leaves the JVM's 1.5 GB plus Python
plus OS about 2 GB of headroom -- enough to page-cache nearly the whole 3.1 GB
store in RAM. 2 vCPU is ample against a 0.05 load average. 80 GB disk is
still ~5x the real steady-state need and comfortably covers a future full
rebuild (~32 GB peak per `CLAUDE.md`) without another resize.

DigitalOcean resize can grow a droplet's disk but never shrink it, so this
has to be a **new droplet**, not a resize of the old one.

## Why a dump-and-load, not a resize or a raw file copy

- Resize keeps the existing 160 GB disk regardless of the new plan, so it
  doesn't get you the smaller disk. It also means downtime on the *existing*
  droplet during the resize, for no benefit here.
- Raw-copying `/var/lib/neo4j/data` while Neo4j is running risks an
  inconsistent copy (files mid-write). Community edition also can't do a
  live online backup -- that needs Enterprise.
- `neo4j-admin database dump` is the supported migration path: a single
  consistent portable archive, safe to copy however you like, loaded with
  `neo4j-admin database load` on the other end. It needs the database
  **stopped** while it runs (`neo4j-admin` refuses on a mounted database),
  but that's ~14 seconds for a store this size, not the minutes an rsync of
  live files might risk.

## State as of this note

Done, on the **old** droplet (`161.35.92.143`):

1. Generated a dedicated migration keypair, `~/.ssh/id_ed25519_migration`
   (not the GitHub deploy key -- kept separate on purpose). Public key:
   ```
   ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILCpvGMqzkoxdjWyHfxOpgr0cNto21Zl2anlZEo7Ognz music-graph-migration-2026-08-19
   ```
2. `systemctl stop neo4j`, `neo4j-admin database dump neo4j --to-path=/var/lib/music-graph/migration --overwrite-destination=true`, `systemctl start neo4j`. Total downtime ~15s. Verified `neo4j` and `music-graph` both back to `active` and `/api/stats` returning 200 afterward.
3. Dump staged at `/var/lib/music-graph/migration/neo4j.dump` on the old droplet, 1,085,911,588 bytes (~1.09 GB, compressed from the 3.3 GB live store).

Not done yet -- blocked on the new droplet existing:

4. Create the new droplet (human step -- no DigitalOcean API access from
   either droplet's shell): `s-2vcpu-4gb`, Ubuntu 24.04, region `ams3`, with
   the public key above added so the old droplet can SSH in as root.
5. From the old droplet: `scp` the dump across, install Neo4j 5 (`5.26.29`,
   matching the source -- `neo4j-admin load` wants a compatible version) and
   the Python deps, `neo4j-admin database load`, install `music-graph.service`
   (copy the unit from `/etc/systemd/system/music-graph.service` and
   `/etc/music-graph.env`, mode 600), verify `/api/stats` and a live search
   both work.
6. Point whoever uses the app at the new IP (no DNS involved -- the viewer
   is reached by bare IP, see `CLAUDE.md`).
7. Destroy the old droplet (human step -- billing action).

## Gotchas to remember

- `neo4j-admin database load` needs the same major.minor Neo4j version as
  the dump was taken with, or newer -- install `5.26.29` (or a later 5.x) on
  the new box, not whatever the package manager defaults to.
- The migration key is **only** for old-droplet-to-new-droplet access during
  this move. Remove it from the new droplet's `authorized_keys` once the cutover is confirmed good and the old droplet is destroyed -- no reason for it to persist.
- `/etc/music-graph.env` (mode 600) holds `NEO4J_PASSWORD`/`APP_PASSWORD` and
  isn't part of the git repo -- it has to be copied or recreated by hand, it
  won't show up from a `git clone`.
