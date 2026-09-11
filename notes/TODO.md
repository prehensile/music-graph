# TODO

Deferred, real work items -- not the step-by-step "next steps" lists inside
individual session notes (those stay where they are, next to the context
that motivated them). Add here when something is decided worth doing but
deliberately not started now; remove once it's done, rather than marking
it done in place, so this file only ever holds what's still outstanding.

## Parse `<artists>` and `<extraartists>` as distinct credit roles

`CREDITED` carries no property distinguishing Discogs' `<artists>` (the
headline credit) from `<extraartists>` (session/production credits) --
`discogs_to_neo4j.py`'s `process_release` has always written both to the
same relationship type with nothing to tell them apart, since the very
first version of the transform. That's invisible until something needs to
answer "who is this release actually *by*": a release credited to the band
Mucc plus ~50 backing musicians -- individually `Artist` nodes, not the
`Group` -- has no way to distinguish the band from a session player once
both are just `CREDITED`, with no ordering to fall back on either (Cypher
gives none over `OPTIONAL MATCH` results, and Neo4j's own traversal order
isn't guaranteed to reflect Discogs' original document order regardless).

Surfaced by `export_search_index.py` needing to show a disambiguating
artist name per Release in `/v2/search`'s results (see
`notes/static-search-downsize-2026-08-19.md`'s 2026-09-11 update) -- that
work shipped two heuristics (prefer `Group`-labelled credits over
`Artist`-labelled ones when any exist, then sort by degree) as a stand-in,
described in full in `export_search_index.py`'s docstring. Both are
proxies for "recognisable," not Discogs' actual credit structure, and can
be wrong (a session player with an inflated degree from an unrelated
shared-name collision could still outrank a real credited artist on a
group-less release, for instance).

The real fix is upstream of the search index entirely:

- `discogs_to_neo4j.py`'s `process_release` should record which XML block
  (`<artists>` vs `<extraartists>`) each credit came from -- a `role`
  property on `CREDITED`, or a split into two relationship types, or at
  minimum a `sequence` property preserving Discogs' own document order
  (which the current CSV-then-bulk-import pipeline discards entirely).
- For the graph already imported, this needs a live migration re-streaming
  the release dump to backfill it, not a full reimport -- the
  `migrate_add_aliases.py` / `migrate_remove_placeholder_labels.py`
  pattern (see CLAUDE.md's Migrating an existing database section).

Not started.
