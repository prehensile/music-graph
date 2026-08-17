#!/usr/bin/env bash
#
# Provision a fresh Ubuntu 24.04 droplet and run a full Discogs -> Neo4j import,
# unattended, using the prefilter path (no release index, no block storage).
#
#   Recommended droplet: s-4vcpu-8gb (8 GB / 4 vCPU / 160 GB SSD)
#   Expected peak disk:   ~35 GB     Expected runtime: ~4-6 hours
#
# Usage, as root on a fresh droplet:
#
#   export DUMP_DATE=20250801
#   export NEO4J_PASSWORD='choose-something'
#   ./provision_droplet.sh 2>&1 | tee -a /var/log/music-graph.log
#
# Long enough to outlive an SSH session, so run it under screen/tmux, or:
#   nohup ./provision_droplet.sh > /var/log/music-graph.log 2>&1 &
#
# Every step is idempotent: completed work is detected and skipped, so it is
# safe to re-run after a failure or disconnect.
#
set -euo pipefail

DUMP_DATE="${DUMP_DATE:?set DUMP_DATE, e.g. 20250801 (see https://discogs-data-dumps.s3.us-west-2.amazonaws.com/index.html)}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:?set NEO4J_PASSWORD}"

DUMP_YEAR="${DUMP_DATE:0:4}"
DATA_DIR="${DATA_DIR:-/var/lib/music-graph/data}"
CSV_DIR="${CSV_DIR:-$DATA_DIR/csv}"
REPO_DIR="${REPO_DIR:-/opt/music-graph}"
REPO_URL="${REPO_URL:-https://github.com/prehensile/music-graph.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
# Community Edition serves exactly one user database, so import into the
# default. Naming it anything else yields a database Neo4j will not start.
DATABASE="${DATABASE:-neo4j}"
BASE_URL="https://discogs-data-dumps.s3.us-west-2.amazonaws.com/data/${DUMP_YEAR}"

log() { printf '\n[%s] == %s\n' "$(date -u +%H:%M:%S)" "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- preflight

preflight() {
    log "Preflight"
    [[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }

    mkdir -p "$DATA_DIR" "$CSV_DIR"

    local avail_gb
    avail_gb=$(df -BG --output=avail "$DATA_DIR" | tail -1 | tr -dc '0-9')
    echo "  free space on $DATA_DIR: ${avail_gb} GB"
    if (( avail_gb < 45 )); then
        echo "  WARNING: under 45 GB free. Peak need is ~35 GB (10 GB of .gz" >&2
        echo "  dumps + ~5 GB CSVs + ~15 GB Neo4j store). Attach a volume or" >&2
        echo "  resize before continuing." >&2
    fi

    # A little swap keeps the importer from being OOM-killed on 8 GB boxes.
    if [[ ! -f /swapfile ]] && (( $(free -g | awk '/^Mem:/{print $2}') < 16 )); then
        log "Creating 4G swapfile"
        fallocate -l 4G /swapfile
        chmod 600 /swapfile
        mkswap /swapfile >/dev/null
        swapon /swapfile
        grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
}

# ---------------------------------------------------------------- packages

install_packages() {
    log "Installing base packages"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq \
        python3 python3-pip python3-venv \
        openjdk-21-jre-headless \
        git curl wget ca-certificates gnupg pv
    python3 --version

    # The transform uses csv.QUOTE_STRINGS, added in Python 3.12. Ubuntu 24.04
    # ships 3.12 as python3, so this should never fire -- but fail loudly rather
    # than a few hours in.
    python3 - <<'PY'
import csv, sys
if not hasattr(csv, "QUOTE_STRINGS"):
    sys.exit(f"Python {sys.version_info.major}.{sys.version_info.minor} is too old; need 3.12+")
PY
}

install_neo4j() {
    if have neo4j-admin; then
        log "Neo4j already installed ($(neo4j-admin --version 2>/dev/null || echo unknown))"
        return
    fi
    log "Installing Neo4j 5 (Community)"
    mkdir -p /etc/apt/keyrings
    wget -qO - https://debian.neo4j.com/neotechnology.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/neotechnology.gpg
    echo "deb [signed-by=/etc/apt/keyrings/neotechnology.gpg] https://debian.neo4j.com stable 5" \
        > /etc/apt/sources.list.d/neo4j.list
    apt-get update -qq
    apt-get install -y -qq neo4j
    systemctl stop neo4j 2>/dev/null || true
    systemctl enable neo4j >/dev/null 2>&1 || true
    neo4j-admin --version
}

fetch_repo() {
    if [[ -d "$REPO_DIR/.git" ]]; then
        log "Updating repo in $REPO_DIR"
        git -C "$REPO_DIR" fetch --quiet origin "$REPO_BRANCH"
        git -C "$REPO_DIR" checkout --quiet "$REPO_BRANCH"
        git -C "$REPO_DIR" reset --hard --quiet "origin/$REPO_BRANCH"
    else
        log "Cloning $REPO_URL"
        git clone --quiet --branch "$REPO_BRANCH" "$REPO_URL" "$REPO_DIR"
    fi
    pip3 install --quiet --break-system-packages -r "$REPO_DIR/requirements.txt"
}

# ---------------------------------------------------------------- dumps

download_dumps() {
    log "Downloading dumps for $DUMP_DATE"
    local checksums="$DATA_DIR/discogs_${DUMP_DATE}_CHECKSUM.txt"

    if [[ ! -s "$checksums" ]]; then
        curl -fsSL --retry 5 --retry-delay 5 \
            "$BASE_URL/discogs_${DUMP_DATE}_CHECKSUM.txt" -o "$checksums" \
            || echo "  no CHECKSUM file retrieved; downloads will not be verified"
    fi

    local kind f url
    for kind in artists labels masters releases; do
        f="$DATA_DIR/discogs_${DUMP_DATE}_${kind}.xml.gz"
        url="$BASE_URL/discogs_${DUMP_DATE}_${kind}.xml.gz"

        if [[ -s "$f" ]] && gzip -t "$f" 2>/dev/null; then
            echo "  have $(basename "$f") ($(du -h "$f" | cut -f1)), skipping"
            continue
        fi

        echo "  fetching $(basename "$f")"
        # -C - resumes a partial download, so a dropped connection is cheap.
        curl -fL --retry 5 --retry-delay 10 -C - "$url" -o "$f" || {
            echo "  FAILED: $url" >&2
            echo "  Check the date exists and the URL pattern still holds:" >&2
            echo "    https://discogs-data-dumps.s3.us-west-2.amazonaws.com/index.html" >&2
            exit 1
        }
        gzip -t "$f" || { echo "  $(basename "$f") is not valid gzip" >&2; exit 1; }
    done

    if [[ -s "$checksums" ]]; then
        log "Verifying checksums"
        ( cd "$DATA_DIR" && grep -E "_(artists|labels|masters|releases)\.xml\.gz" "$checksums" \
            | sha256sum -c - ) || {
                echo "  checksum mismatch -- delete the offending file and re-run" >&2
                exit 1
            }
    fi
    du -sh "$DATA_DIR"
}

# ---------------------------------------------------------------- transform

run_transform() {
    if [[ -s "$CSV_DIR/releases.csv" && -s "$CSV_DIR/labels.csv" ]]; then
        log "CSVs already present in $CSV_DIR, skipping transform"
        return
    fi

    log "Transforming dumps to import CSVs (the long step)"
    # The transform refuses a non-empty output folder, since its writers append.
    rm -f "$CSV_DIR"/*.csv
    rm -f "$DATA_DIR/labels.db"

    # --release-xml streams the .gz directly, keeping only the ~3M releases that
    # are some master's main_release. No decompression, no index, one pass.
    python3 "$REPO_DIR/discogs_to_neo4j.py" \
        --artist-xml  "$DATA_DIR/discogs_${DUMP_DATE}_artists.xml.gz" \
        --label-xml   "$DATA_DIR/discogs_${DUMP_DATE}_labels.xml.gz" \
        --master-xml  "$DATA_DIR/discogs_${DUMP_DATE}_masters.xml.gz" \
        --release-xml "$DATA_DIR/discogs_${DUMP_DATE}_releases.xml.gz" \
        --label-sqlite "$DATA_DIR/labels.db" \
        --output-folder "$CSV_DIR"

    log "Exporting deduplicated labels"
    python3 "$REPO_DIR/sqlite_to_csv.py" \
        --sqlite-path "$DATA_DIR/labels.db" \
        --output-folder "$CSV_DIR"

    log "Checking node CSVs for duplicate IDs"
    local n
    for n in artists groups releases labels; do
        printf '  %-10s ' "$n"
        python3 "$REPO_DIR/dupe_finder.py" "$CSV_DIR/$n.csv" | head -1
    done

    wc -l "$CSV_DIR"/*.csv
}

# ---------------------------------------------------------------- import

import_graph() {
    log "Importing into Neo4j database '$DATABASE'"
    systemctl stop neo4j 2>/dev/null || true

    # neo4j-admin runs as the neo4j user and must be able to read the CSVs, which
    # means every parent directory has to be traversable too.
    chown -R neo4j:neo4j "$CSV_DIR"
    chmod a+rX "$CSV_DIR"/*.csv
    local d="$CSV_DIR"
    while [[ "$d" != "/" ]]; do chmod a+x "$d"; d=$(dirname "$d"); done

    sudo -u neo4j "$REPO_DIR/neo4j_admin_import.sh" "$CSV_DIR" "$DATABASE"

    log "Setting initial password"
    neo4j-admin dbms set-initial-password "$NEO4J_PASSWORD" 2>/dev/null \
        || echo "  password already set, leaving it alone"

    log "Starting Neo4j"
    systemctl start neo4j
    local i
    for i in $(seq 1 60); do
        if curl -fsS -o /dev/null http://localhost:7474/ 2>/dev/null; then
            echo "  up after ${i}0s"; break
        fi
        sleep 10
    done
}

verify() {
    log "Verifying the graph"
    # cypher-shell reads NEO4J_USERNAME/NEO4J_PASSWORD from the environment, so
    # the password never appears in the process list or shell history.
    export NEO4J_USERNAME=neo4j NEO4J_PASSWORD
    cypher-shell -d "$DATABASE" \
        "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS nodes ORDER BY nodes DESC;" \
        || { echo "  could not query -- check: journalctl -u neo4j -n 50" >&2; return 1; }
    cypher-shell -d "$DATABASE" \
        "MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS count ORDER BY count DESC;" || true
}

# ---------------------------------------------------------------- main

main() {
    local started=$SECONDS
    preflight
    install_packages
    install_neo4j
    fetch_repo
    download_dumps
    run_transform
    import_graph
    verify

    log "Done in $(( (SECONDS - started) / 60 )) minutes"
    cat <<EOF

Bolt is bound to localhost only, which is the right default -- do not open 7687
or 7474 to the internet. Reach it over an SSH tunnel from your machine:

    ssh -N -L 7687:localhost:7687 -L 7474:localhost:7474 root@<droplet-ip>

Then browse http://localhost:7474 (user neo4j), or serve the viewer locally:

    cd web && cp config.example.js config.js   # set neo4jPassword
    python3 -m http.server 8000

When you are finished, snapshot or destroy the droplet -- it bills by the hour.
EOF
}

main "$@"
