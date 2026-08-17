#!/usr/bin/env bash
#
# Bulk-import the generated CSVs into a Neo4j database.
#
# Neo4j must be STOPPED before running this. Run it from the Neo4j home
# directory, or with neo4j-admin on PATH.
#
# Usage: ./neo4j_admin_import.sh <csv-folder> [database-name]
#   e.g. ./neo4j_admin_import.sh data/2025-07-28 discogs
#
set -euo pipefail

CSV_DIR="${1:?usage: $0 <csv-folder> [database-name]}"
DATABASE="${2:-discogs}"

for f in artists.csv groups.csv labels.csv releases.csv \
         artist_group_links.csv artist_release_links.csv \
         label_sublabel_links.csv release_label_links.csv; do
    if [[ ! -f "$CSV_DIR/$f" ]]; then
        echo "Missing $CSV_DIR/$f -- run the transform steps first." >&2
        exit 1
    fi
done

# --multiline-fields is required because Discogs profile text contains newlines.
#
# Add --skip-bad-relationships=true if the import aborts on dangling SUBLABEL
# rows: sublabels are only written to label_sublabel_links.csv, on the
# assumption that every sublabel also appears as a top-level <label> in the
# labels dump. That holds for full dumps but not for partial ones.
neo4j-admin database import full "$DATABASE" \
    --nodes=Artist="$CSV_DIR/artists.csv" \
    --nodes=Group="$CSV_DIR/groups.csv" \
    --nodes=Label="$CSV_DIR/labels.csv" \
    --nodes=Release="$CSV_DIR/releases.csv" \
    --relationships=MEMBER_OF="$CSV_DIR/artist_group_links.csv" \
    --relationships=CREDITED="$CSV_DIR/artist_release_links.csv" \
    --relationships=SUBLABEL="$CSV_DIR/label_sublabel_links.csv" \
    --relationships=RELEASED_ON="$CSV_DIR/release_label_links.csv" \
    --multiline-fields=true \
    --overwrite-destination
