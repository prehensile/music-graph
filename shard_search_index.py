#!/usr/bin/env python3
"""
Shard search_index_export.csv.gz into small per-prefix JSON files for a
static, browser-only search index -- the "shard the name index by prefix"
step from notes/static-search-downsize-2026-08-19.md.

Two-phase build so peak memory stays flat regardless of the ~15.16M row
input. Worth being deliberate about here specifically: the droplet this was
built and run on is the downsized s-2vcpu-4gb box (`free -h` showed 3.8 GiB
total, ~1.2 GiB available), and holding all 15.16M rows as Python objects
at once would plausibly run into several GB by itself.

Phase 1 (partition): stream the export once, normalise each name (Unicode
NFKD decompose + strip combining marks + casefold, so e.g. "Bjork"/"Björk"
land in the same bucket), and append each row -- as a compact JSON line --
to one of --partitions bucket files, chosen by hashing the row's
*normalised* 2-char prefix. Every row for a given prefix always lands in
the same partition file, but a partition file holds many different
prefixes, so partition count stays fixed and small regardless of how skewed
the real prefix distribution is.

Phase 2 (finalise): each partition file is now small enough (~1/partitions
of the total) to load fully into memory. Group its rows by exact prefix;
any prefix bucket over --max-shard-rows is recursively split one character
deeper (bounded by --max-prefix-len) until every resulting shard is small
-- tens of KB, per the note -- rather than shipping one huge file for a
common prefix like "the" (564,631 rows on the 2026-08 dump, well past any
sane single fetch, and that's real name distribution, not the "Not On
Label" placeholder skew fixed elsewhere -- see CLAUDE.md's Data Model
section). Each final shard is written as compact JSON: an array of
[type, id, name, degree] rows, not objects -- no repeated key names against
15M rows.

No manifest lists where each shard lives, deliberately. A shard's filename
*is* its (URL-encoded) prefix -- see shard_filename -- so the browser needs
no lookup at all: try "<encode(typed[:2])>.json"; a 404 means that prefix
was too big to ship whole and got split deeper, so wait for a 3rd character
and try "<encode(typed[:3])>.json", and so on. An earlier version generated
a manifest.json listing every shard's prefix/file/count, meant to be the
thing the browser consulted to find the right file -- it came out at
12-18MB, bigger than any shard it was meant to help find, i.e. exactly the
whole-corpus-sized fetch sharding was meant to avoid. manifest.json still
gets written, but only as build metadata (row/shard counts, the params
used) -- nothing in it is needed at request time.

    python shard_search_index.py search_index_export.csv.gz -o web/search-index

Not wired into app.js yet -- this only produces the on-disk shards. Whatever
fetch code lands there needs to reproduce shard_filename's encoding exactly
(see its docstring) -- encodeURIComponent alone is not a safe substitute.
"""

import argparse
import csv
import gzip
import hashlib
import json
import os
import shutil
import sys
import unicodedata
from collections import defaultdict
from urllib.parse import quote

from progress import Heartbeat

# type -> single-char code, to keep shard files small; decoded client-side
# via manifest["types"].
TYPE_CODES = {"Artist": "A", "Group": "G", "Release": "R", "Label": "L"}

PREFIX_LEN = 2  # initial partitioning depth; finalise() may split deeper


def normalise(name):
    """Casefold + strip accents, so search is accent- and case-insensitive.

    NFKD decomposes e.g. "é" into "e" + a combining acute accent; dropping
    combining marks then leaves plain "e". casefold() rather than lower()
    for the same reason server.py's clean_term doesn't matter here but
    would elsewhere -- it's the more thorough Unicode-aware fold.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold()


def open_input(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", newline="", encoding="utf-8")
    return open(path, "r", newline="", encoding="utf-8")


def shard_filename(prefix):
    """
    URL-safe filename for a shard, named by its own prefix rather than a
    sequential index -- so the client can compute the fetch path directly
    from what's been typed, with no lookup file of any kind: try
    "<encode(typed[:2])>.json"; a 404 means that prefix was too big to ship
    whole and got split deeper (see split_bucket), so wait for a 3rd
    character and try "<encode(typed[:3])>.json", and so on. The absence of
    a file *is* the "look deeper" signal -- no manifest, no redirect stub.

    The encoding matters more than it looks: it has to be reproduced
    *exactly* by whatever fetch code app.js eventually gets, or a mismatch
    would look identical to a genuine miss. JS's built-in
    encodeURIComponent is NOT safe to pair with this -- it leaves
    `! * ' ( )` unescaped while urllib.parse.quote does not, and prefixes
    can genuinely start with those (e.g. the real Discogs artist
    "(hed) Planet Earth"). Whatever client code lands later must use a
    matching encoder, not encodeURIComponent directly.
    """
    return quote(prefix, safe="") + ".json"


def partition_key(prefix, n_partitions):
    """Stable hash -> partition index. Not Python's hash(): that's salted
    per-process, which would make a partition file's contents depend on
    the run rather than just the prefix -- harmless here since phase 1/2
    run in one process, but stdlib hash() being unicode-safe and easy to
    reason about matters more than speed at this data size."""
    digest = hashlib.blake2b(prefix.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big") % n_partitions


def partition(input_path, work_dir, n_partitions):
    """Phase 1: stream the CSV once, bucket rows into N partition files."""
    handles = [
        open(os.path.join(work_dir, f"part_{i:04d}.jsonl"), "w", encoding="utf-8")
        for i in range(n_partitions)
    ]
    hb = Heartbeat("partition", unit="rows")
    hb.begin()
    written = skipped = 0
    try:
        with open_input(input_path) as fh:
            reader = csv.reader(fh)
            header = next(reader)
            assert header == ["type", "id", "name", "degree"], f"unexpected header: {header}"
            for row in reader:
                type_, id_, name, degree = row
                code = TYPE_CODES.get(type_)
                if code is None:
                    skipped += 1
                    continue
                key = normalise(name)
                prefix = key[:PREFIX_LEN]
                fh_out = handles[partition_key(prefix, n_partitions)]
                fh_out.write(json.dumps([prefix, code, id_, name, int(degree)]) + "\n")
                written += 1
                hb.tick(written)
    finally:
        for fh_out in handles:
            fh_out.close()
    hb.finish(f"{skipped:,} skipped (unknown type)")
    return written


def split_bucket(prefix, rows, max_shard_rows, max_prefix_len, shards):
    """Recursively split one (prefix -> rows) bucket until every leaf is
    small, or max_prefix_len is hit (accepted oversized rather than
    recursing forever -- true for e.g. many exact-duplicate titles).

    A row whose normalised key is no longer than `prefix` (a short name,
    or an exact duplicate of one) can't be split any deeper -- key[:n] on
    a shorter string just returns the string unchanged, so its "next"
    prefix would equal `prefix` again. Recursing on that would never
    shrink the bucket and never lengthen the prefix, i.e. it would never
    terminate. Those rows are shunted straight into the output instead of
    being recursed on; only rows whose prefix actually got longer recurse
    further, which guarantees termination well before max_prefix_len.
    """
    if len(rows) <= max_shard_rows or len(prefix) >= max_prefix_len:
        shards.append((prefix, rows))
        return
    deeper = defaultdict(list)
    for row in rows:
        key = row[0]  # normalised full key, stashed alongside each row below
        deeper[key[: len(prefix) + 1]].append(row)
    for sub_prefix, sub_rows in deeper.items():
        if sub_prefix == prefix:
            shards.append((sub_prefix, sub_rows))
        else:
            split_bucket(sub_prefix, sub_rows, max_shard_rows, max_prefix_len, shards)


def finalise(work_dir, output_dir, n_partitions, max_shard_rows, max_prefix_len, total_rows):
    """Phase 2: read each partition file (small), group exact-match
    prefixes, split any oversized ones deeper, write final shard JSON.

    No file is written for a prefix that got split deeper -- only leaves
    become shards. That absence is deliberate, not an oversight: see
    shard_filename for how the client is meant to use a 404 as the signal
    to try a longer prefix, instead of consulting a manifest.
    """
    os.makedirs(output_dir, exist_ok=True)
    hb = Heartbeat("finalise", total=n_partitions, unit="partitions")
    hb.begin()

    shard_count = 0
    written_files = set()  # leaf prefixes are unique by construction (see
    # split_bucket's docstring) -- this just catches a violation of that
    # loudly instead of one shard silently overwriting another's file.
    for i in range(n_partitions):
        part_path = os.path.join(work_dir, f"part_{i:04d}.jsonl")
        # bucket[normalised_2char_prefix] -> list of
        # [normalised_key, type_code, id, name, degree]
        buckets = defaultdict(list)
        with open(part_path, "r", encoding="utf-8") as fh:
            for line in fh:
                prefix, code, id_, name, degree = json.loads(line)
                key = normalise(name)  # full key, not just the 2-char prefix
                buckets[prefix].append([key, code, id_, name, degree])

        for prefix, rows in buckets.items():
            leaves = []
            split_bucket(prefix, rows, max_shard_rows, max_prefix_len, leaves)
            for leaf_prefix, leaf_rows in leaves:
                filename = shard_filename(leaf_prefix)
                assert filename not in written_files, f"duplicate shard filename: {filename!r}"
                written_files.add(filename)
                # Drop the normalised key before writing -- it was only
                # needed to decide which shard a row belongs in.
                compact = [[code, id_, name, degree] for _, code, id_, name, degree in leaf_rows]
                with open(os.path.join(output_dir, filename), "w", encoding="utf-8") as out:
                    json.dump(compact, out, separators=(",", ":"), ensure_ascii=False)
                shard_count += 1
        hb.tick(i + 1)
    hb.finish()

    # Build metadata only -- not fetched by the client, which finds its
    # shard by trying "<encoded prefix>.json" directly (see shard_filename)
    # rather than consulting an index. Kept small deliberately: an earlier
    # version listed every shard here and it came out at 12-18MB, bigger
    # than any shard it was meant to help find and a whole-corpus fetch in
    # everything but name -- exactly what sharding was meant to avoid.
    manifest = {
        "generated_from": "search_index_export.csv.gz",
        "total_rows": total_rows,
        "shard_count": shard_count,
        "max_shard_rows": max_shard_rows,
        "max_prefix_len": max_prefix_len,
        "types": {v: k for k, v in TYPE_CODES.items()},
    }
    with open(os.path.join(output_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, separators=(",", ":"), ensure_ascii=False)
    return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("input", nargs="?", default="search_index_export.csv.gz",
                     help="output of export_search_index.py (default: %(default)s)")
    ap.add_argument("-o", "--output-dir", default="web/search-index",
                     help="where to write shard_*.json + manifest.json (default: %(default)s)")
    ap.add_argument("--partitions", type=int, default=64,
                     help="phase-1 bucket file count (default: %(default)s)")
    ap.add_argument("--max-shard-rows", type=int, default=2000,
                     help="split a prefix bucket deeper once it exceeds this many rows "
                          "(default: %(default)s)")
    ap.add_argument("--max-prefix-len", type=int, default=24,
                     help="stop splitting deeper at this prefix length even if still "
                          "oversized -- a safety valve, not expected to bind in practice "
                          "(split_bucket's no-progress guard already terminates "
                          "recursion on its own); needs to be generous enough that real "
                          "long-shared-prefix data (e.g. Discogs' 'Not On Label (Artist "
                          "Name)' convention for unofficial releases) still splits on the "
                          "part that actually differs (default: %(default)s)")
    ap.add_argument("--work-dir", default=None,
                     help="scratch dir for phase-1 partition files (default: "
                          "<output-dir>/.partitions, removed on success)")
    args = ap.parse_args()

    if not os.path.exists(args.input):
        sys.exit(f"input not found: {args.input}")

    work_dir = args.work_dir or os.path.join(args.output_dir, ".partitions")
    os.makedirs(work_dir, exist_ok=True)

    total_rows = partition(args.input, work_dir, args.partitions)
    manifest = finalise(
        work_dir, args.output_dir, args.partitions,
        args.max_shard_rows, args.max_prefix_len, total_rows,
    )

    shutil.rmtree(work_dir, ignore_errors=True)

    print(
        f"\n{total_rows:,} rows -> {manifest['shard_count']:,} shards in {args.output_dir}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
