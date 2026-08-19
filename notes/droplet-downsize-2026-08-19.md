# Droplet downsize: 8GB → 4GB, 2026-08-19

**Status: migration complete.** New droplet `188.166.61.98` (`s-2vcpu-4gb`,
region `ams3`) is live and verified against the old one — see §3. Old
droplet `161.35.92.143` (`s-4vcpu-8gb`) is still up as of this note; §4 is
what's left before it can be destroyed.

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

## 1. What was done, on the old droplet (`161.35.92.143`)

1. Generated a dedicated migration keypair, `~/.ssh/id_ed25519_migration`
   (not the GitHub deploy key -- kept separate on purpose).
2. `systemctl stop neo4j`, `neo4j-admin database dump neo4j --to-path=/var/lib/music-graph/migration --overwrite-destination=true`, `systemctl start neo4j`. Total downtime ~15s. Verified `neo4j` and `music-graph` both back to `active` and `/api/stats` returning 200 afterward.
3. Dump staged at `/var/lib/music-graph/migration/neo4j.dump`, 1,085,911,588 bytes (~1.09 GB, compressed from the 3.3 GB live store).

## 2. Setting up the new droplet (`188.166.61.98`)

Once the droplet existed with the migration key in `authorized_keys` (human
step -- no DigitalOcean API access from either droplet's shell), everything
else ran from the old droplet over SSH:

1. Base packages, matching `provision_droplet.sh`'s `install_packages`: `python3 python3-pip python3-venv openjdk-21-jre-headless git curl wget ca-certificates gnupg ufw`. Got Python 3.12.3.
2. 4G swapfile (same `< 16GB total RAM` rule `provision_droplet.sh` uses).
3. Neo4j from `debian.neo4j.com`, **pinned to `neo4j=1:5.26.29`** rather than whatever `stable 5` resolves to today -- exact version match to the dump's source, sidestepping any "is a newer/older 5.x compatible" question entirely. It was available in the repo, so no compromise needed.
4. `ufw`: 22 and 80 only, same as `configure_firewall`.
5. `neo4j-admin dbms set-initial-password "$NEO4J_PASSWORD"` (same password as the old droplet's `/etc/music-graph.env`) -- **before** the first `neo4j` start, since that's what initialises the `system` database's auth. A dump/load only carries the named database (`neo4j`), never `system`, so this step is not optional.
6. `scp` the dump across (1.09 GB in ~7s, DO's internal network), `neo4j-admin database load neo4j --from-path=... --overwrite-destination=true` (~15s), `systemctl start neo4j`.
7. **Hit an `AccessDeniedException` on first start**: the load ran as root, so `/var/lib/neo4j/data` came out root-owned, and the `neo4j` service user (which the systemd unit runs as) couldn't open its own store files. Fixed with `systemctl stop neo4j && chown -R neo4j:neo4j /var/lib/neo4j/data && systemctl start neo4j`. Worth remembering if a load-then-start ever fails with "Failed to read current store version" / `AccessDeniedException` on `neostore` -- it reads like a corrupt store but is just ownership.
8. `git clone` (public repo, no token needed) at the exact same commit the old droplet was on, `pip install -r requirements.txt`, `./fetch_vendor.sh`.
9. `/etc/music-graph.env` and `/etc/systemd/system/music-graph.service` recreated by hand (paths are identical between the two droplets, so this was a straight copy of the values, not a translation) -- these aren't in git, `install_app` in `provision_droplet.sh` is what originally generates them, and a `git clone` alone won't produce them.
10. Deleted the staged dump on both ends once verified (§3), reclaiming ~1 GB each.

## 3. Verification (passed)

| | old (`161.35.92.143`) | new (`188.166.61.98`) |
|---|---|---|
| `/api/stats` | `{"Artist": 9498432, "Group": 664886, "Release": 2579769, "Label": 2420830}` | identical |
| Node count | 15,163,917 | 15,163,917 |
| Relationship count | 19,415,901 | 19,415,901 |
| `entitySearch` index | — | `ONLINE`, 100% populated, came across with the dump -- no rebuild needed |
| `"goldie timeless"` search-relevance fix | works | works (confirms `_rank_matches` logic, not just data, is intact) |
| Static assets (`/`, `/app.js`, `/styles.css`, `/vendor/*`) | — | all 200 |
| Basic auth | — | wrong credentials correctly 401 |
| Memory after warm-up | JVM RSS 1.46 GB | JVM RSS ~0.6 GB (smaller default heap on the smaller box -- JVM ergonomics scale with available RAM), 1.1 GB used of 3.8 GB total, 2.7 GB available |

## 4. What's left (human steps)

1. **Confirm in a browser**: `http://188.166.61.98/` -- search, tap a node, check the live-search dropdown.
2. Point whoever uses the app at the new IP (no DNS involved -- the viewer is reached by bare IP, see `CLAUDE.md`).
3. Remove the migration key from the new droplet's `authorized_keys` -- it was only ever meant for this move:
   ```
   ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILCpvGMqzkoxdjWyHfxOpgr0cNto21Zl2anlZEo7Ognz music-graph-migration-2026-08-19
   ```
4. Destroy the old droplet (`161.35.92.143`) -- billing action, do this last, once step 1 is confirmed good.

## Gotchas to remember

- `neo4j-admin database load` needs the same major.minor Neo4j version as
  the dump was taken with, or newer. Pinning the exact version (`5.26.29`)
  removed any doubt rather than trusting `stable 5`'s latest.
- A dump/load only migrates the named database, never `system` -- the
  password has to be set independently on the new box, before first start.
- `neo4j-admin database load` run as root leaves the store root-owned; the
  `neo4j` systemd user then can't open it. `chown -R neo4j:neo4j` before
  starting, or right after a failed first start.
- The migration key is **only** for old-droplet-to-new-droplet access during
  this move -- see step 3 above.
- `/etc/music-graph.env` (mode 600) holds `NEO4J_PASSWORD`/`APP_PASSWORD` and
  isn't part of the git repo -- it has to be copied or recreated by hand, it
  won't show up from a `git clone`.
