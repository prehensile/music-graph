/**
 * Client for the static, sharded search index built by
 * export_search_index.py / shard_search_index.py (see
 * notes/static-search-downsize-2026-08-19.md). Talks only to plain static
 * files under /search-index/ -- no /api/*, no server.py involvement at all.
 * This is deliberately a standalone module: nothing here is imported by, or
 * shared with, app.js. See search.html for the (also standalone) test page
 * built on top of it.
 *
 * The whole design bet this module exercises: a shard's filename *is* its
 * (URL-encoded) prefix, so finding the right shard for a typed query needs
 * no manifest lookup -- try "<prefix>.json"; a 404 means that prefix was
 * too big to ship whole and was split deeper at build time, so retry one
 * character longer. See shard_search_index.py's module docstring and
 * shard_filename() for the build side of this contract.
 *
 * Known limitation, not a bug: this only ever does a single contiguous
 * prefix match against a node's own name -- "goldie timeless" (the
 * motivating example in CLAUDE.md's Known Issues / entitySearch section)
 * finds nothing here, because Goldie's 1995 album "Timeless" doesn't
 * itself start with "goldie timeless"; that match needs the query split
 * into terms and reasoned about across the CREDITED relationship, exactly
 * what server.py's Graph._rank_matches neighbour-boost exists for. This
 * flat index carries only (type, id, name, degree) per row -- no
 * relationship data of any kind -- so nothing here can do that. Whatever
 * replaces the neighbour-boost client-side (see
 * notes/static-search-downsize-2026-08-19.md's next steps) is still open.
 */

const SHARD_BASE = "/search-index/";
const MANIFEST_URL = SHARD_BASE + "manifest.json";

/**
 * Normalise a name the same way shard_search_index.py's normalise() does at
 * build time: NFKD-decompose (splits an accented character into its base
 * letter plus a combining mark), strip combining marks, casefold. Two names
 * that normalise to the same string land in the same shard, e.g.
 * "Bjork"/"Björk".
 *
 * Not a byte-perfect match to Python's str.casefold() -- JS has no built-in
 * equivalent, so this uses toLowerCase(). The two agree for the vast
 * majority of scripts; the known gap is characters with special casefolding
 * (German "ß" casefolds to "ss" in Python, toLowerCase() leaves it as "ß"),
 * which could occasionally look for a shard under a slightly different key
 * than the build put it under. Acceptable for this test harness; closing it
 * fully would need a casefold table, not just toLowerCase().
 */
export function normalise(name) {
  const decomposed = (name || "").normalize("NFKD");
  const stripped = decomposed.replace(/\p{Mn}/gu, "");
  return stripped.toLowerCase();
}

/**
 * Percent-encode exactly the way Python's urllib.parse.quote(s, safe="")
 * does -- NOT the same as encodeURIComponent alone. Both operate on UTF-8
 * bytes and agree on every character except five encodeURIComponent leaves
 * unescaped that Python does not: ! * ' ( ). Escaping those five afterwards
 * makes the two outputs byte-for-byte identical (checked directly against
 * quote() output, not just against the ECMAScript/RFC3986 spec text: e.g.
 * quote("(hed) planet earth", safe="") ==
 * "%28hed%29%20planet%20earth", quote("björk", safe="") == "bj%C3%B6rk").
 * A prefix genuinely can start with one of these -- "(hed) Planet Earth" is
 * a real Discogs artist -- so this isn't a hypothetical edge case.
 */
export function pyQuote(s) {
  return encodeURIComponent(s).replace(/[!'()*]/g, (c) =>
    "%" + c.charCodeAt(0).toString(16).toUpperCase()
  );
}

// Must match shard_search_index.py's SLASH_SUBSTITUTE exactly: "/" can
// never survive as part of a single on-disk filename (see that constant's
// comment for why -- http.server treats a decoded "/" as a directory
// separator no matter how it arrived), so the build substitutes this
// character for it before writing the file. This has to apply that same
// substitution before percent-encoding, or a prefix containing "/" (e.g.
// "AC/DC") would request a filename the build never wrote.
//
// U+E000, the first Private Use Area codepoint -- chosen there (and
// mirrored here) only after U+2044 FRACTION SLASH collided with real
// names on the first real build: NFKD decomposition, which normalise()
// applies before this substitution ever runs, expands a vulgar fraction
// like "½" into "1⁄2", so that "distinct" character was already reachable
// from ordinary text. A PUA codepoint has no decomposition and no defined
// meaning in real text, so it can't be produced by any route a genuine
// name would take.
const SLASH_SUBSTITUTE = "";

/**
 * The URL path segment for a shard -- percent-encoded for the request, NOT
 * the same string as the file's name on disk. shard_search_index.py writes
 * shards under their raw, unencoded prefix (see its shard_filename
 * docstring for exactly why conflating the two silently broke 61% of
 * shards on the first real build); this function's job is only to build a
 * valid URL that, once the server unquotes it, lands back on that literal
 * filename.
 */
export function shardFilename(prefix) {
  return pyQuote(prefix.replaceAll("/", SLASH_SUBSTITUTE)) + ".json";
}

// manifest.json is build metadata (total row/shard counts, the type-code ->
// label mapping, the params the build used) -- fetched once and cached, but
// never consulted to find a shard. See shard_search_index.py's docstring
// for why: an earlier version of the build listed every shard's location in
// it, and it came out at 12-18MB, bigger than any shard it existed to help
// find.
let manifestPromise = null;
export function loadManifest() {
  if (!manifestPromise) {
    manifestPromise = fetch(MANIFEST_URL, { cache: "no-store" }).then((res) => {
      if (!res.ok) throw new Error(`manifest fetch failed: HTTP ${res.status}`);
      return res.json();
    });
  }
  return manifestPromise;
}

// filename -> Promise<Array|null>, null meaning "confirmed 404". Shared
// across every search() call in the page, so widening from "th" to "the" to
// "the " as the user keeps typing re-fetches nothing already resolved,
// hit or miss.
const shardCache = new Map();

function fetchShard(prefix, onFetch) {
  const filename = shardFilename(prefix);
  if (shardCache.has(filename)) {
    if (onFetch) onFetch({ prefix, filename, cached: true });
    return shardCache.get(filename);
  }
  const startedAt = performance.now();
  const promise = fetch(SHARD_BASE + filename, { cache: "no-store" }).then((res) => {
    const ms = performance.now() - startedAt;
    if (res.status === 404) {
      if (onFetch) onFetch({ prefix, filename, status: 404, ms });
      return null;
    }
    if (!res.ok) {
      throw new Error(`shard fetch failed (HTTP ${res.status}): ${filename}`);
    }
    const bytes = Number(res.headers.get("content-length")) || null;
    return res.json().then((rows) => {
      if (onFetch) onFetch({ prefix, filename, status: 200, ms, rows: rows.length, bytes });
      return rows;
    });
  });
  shardCache.set(filename, promise);
  return promise;
}

/**
 * Fetch every shard that could hold a match for a normalised query key --
 * prefix lengths 2, 3, 4, ... up to key.length (or just 1 if the whole key
 * is a single character) -- and return the rows from every one that hits
 * (not just the first).
 *
 * Trying only the first hit and stopping there was the obvious design and
 * is wrong: when a prefix bucket is too big and gets split deeper (see
 * shard_search_index.py's split_bucket), rows whose *whole* normalised
 * name is exactly that prefix -- e.g. a real artist literally named "BJ"
 * -- get written as their own small leaf at the *shallow* depth, sitting
 * alongside the deeper files split_bucket also wrote for longer names
 * sharing that prefix. So a 200 at depth 2 doesn't mean "this is the
 * complete answer for this prefix" -- it can just as easily mean "this is
 * only the exact-length leftover; the real matches are one or more
 * characters deeper." Concretely, searching "bjork" against the 2026-09
 * build: bj.json exists (2 rows, entities literally named "BJ"), bjo.json
 * exists (2 more, "BJO"), bjor.json doesn't exist, and bjork.json exists
 * (65 rows -- Björk and friends). Stopping at the first hit (bj.json)
 * would silently return zero results for "bjork" despite a real match
 * existing two characters deeper -- caught only by checking an actual
 * query end-to-end through the live server, not by checking that shard
 * requests individually returned the right HTTP status.
 *
 * The trade lower down is more requests per query (up to key.length,
 * mostly fast 404s) for actually-correct results -- fetched in parallel,
 * not sequentially, since there's no early-exit to save a round trip for
 * once every depth needs checking anyway.
 */
async function findShards(key, onFetch) {
  const start = Math.min(2, key.length);
  const lengths = [];
  for (let len = start; len <= key.length; len += 1) lengths.push(len);

  const results = await Promise.all(
    lengths.map((len) => {
      const prefix = key.slice(0, len);
      return fetchShard(prefix, onFetch).then((rows) => ({ prefix, rows }));
    })
  );
  return results.filter((r) => r.rows !== null);
}

/**
 * Search the sharded index for `text`. Resolves to:
 *   { key, matches, shardsHit, fetches }
 * - key: the normalised query actually searched for.
 * - matches: [{ type, id, name, degree, artists, date }], sorted by degree
 *   descending (same "well-connected first" tie-break server.py's
 *   _rank_matches uses), capped at `limit`. artists and date are "" for
 *   everything except Release -- without artists there's no way to tell
 *   e.g. the several dozen different releases all titled "Timeless" apart
 *   in a result list (see export_search_index.py's RELEASE_ARTIST_CAP and
 *   its Group-over-Artist-credit preference for how the shown name(s) are
 *   chosen); date is passed through from Discogs' <released> verbatim, not
 *   reformatted.
 * - shardsHit: the prefixes of every shard file that actually contributed
 *   rows -- usually one, but see findShards for why a query can
 *   legitimately need more than one shard merged together.
 * - fetches: every shard request this call made (including cache hits and
 *   404s), for a diagnostics view -- see search.html.
 *
 * Every hit shard's rows are pooled and filtered against the *full* key
 * before anything is returned, not just the deepest/last one found -- a
 * shard can cover more ground than the query itself (e.g. a query of
 * "the ma" resolves to the "the" shard if "the" was never split, which
 * contains plenty of rows that don't start with "the ma"), so this
 * filter is doing real work, not just being defensive.
 */
export async function search(text, { limit = 50 } = {}) {
  const key = normalise(text || "").trim();
  const fetches = [];
  const onFetch = (entry) => fetches.push(entry);

  if (!key) return { key, matches: [], shardsHit: [], fetches };

  const hits = await findShards(key, onFetch);
  if (!hits.length) return { key, matches: [], shardsHit: [], fetches };

  const manifest = await loadManifest();
  const types = manifest.types || {};

  const matches = hits
    .flatMap((h) => h.rows)
    .filter((row) => normalise(row[2]).startsWith(key))
    .sort((a, b) => (b[3] || 0) - (a[3] || 0))
    .slice(0, limit)
    .map(([code, id, name, degree, artists, date]) => (
      { type: types[code] || code, id, name, degree, artists, date }
    ));

  return { key, matches, shardsHit: hits.map((h) => h.prefix), fetches };
}
