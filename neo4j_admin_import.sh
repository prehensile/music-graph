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

# --multiline-fields is required because Discogs profile text contains newlines:
# a label profile can run to several lines inside one quoted field.
#
# neo4j-admin writes its bad-rows report to "import.report" *relative to the
# current directory*, and provisioning runs from / (cloud-init's cwd, which
# sudo -u does not change), where the unprivileged neo4j user cannot write.
# Name the path explicitly, and cd somewhere writable in case anything else is
# resolved relative to cwd.
REPORT_FILE="${REPORT_FILE:-$CSV_DIR/import.report}"
cd "$CSV_DIR"

# Artists and groups share one ID space, and sublabels are registered as Label
# nodes, so essentially every edge should resolve. The tolerance is for genuine
# dump drift -- a release crediting an artist id that is absent from the artists
# dump, say -- and is set low on purpose: an earlier run aborted here with
# 250,004 bad rows, which was the importer correctly refusing a real modelling
# bug rather than a threshold that needed raising.
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
    --skip-bad-relationships=true \
    --bad-tolerance=50000 \
    --report-file="$REPORT_FILE" \
    --overwrite-destination

# Surface anything that was skipped: silence here means a clean import.
if [[ -s "$REPORT_FILE" ]]; then
    echo
    echo "$(wc -l < "$REPORT_FILE") rows were skipped; first few:"
    head -5 "$REPORT_FILE"
    echo "full report: $REPORT_FILE"
fi
