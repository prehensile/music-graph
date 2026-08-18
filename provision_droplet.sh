#!/usr/bin/env bash
#
# Provision a fresh Ubuntu 24.04 droplet: build the graph from the Discogs
# dumps, import it into Neo4j, and serve the viewer over HTTP.
#
#   Recommended droplet: s-4vcpu-8gb (8 GB / 4 vCPU / 160 GB SSD)
#   Expected peak disk:   ~35 GB     Expected runtime: ~4-6 hours
#
# Usage, as root on a fresh droplet:
#
#   export DUMP_DATE=20260801
#   export NEO4J_PASSWORD='choose-something'
#   export APP_PASSWORD='choose-something-else'
#   ./provision_droplet.sh
#
# Progress goes to /var/log/music-graph.log, with the current stage on one line
# in /var/log/music-graph.status and any failure recorded in both. Every step is
# idempotent, so it is safe to re-run after a failure or disconnect.
#
set -euo pipefail

DUMP_DATE="${DUMP_DATE:?set DUMP_DATE, e.g. 20260801 (see https://discogs-data-dumps.s3.us-west-2.amazonaws.com/index.html)}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:?set NEO4J_PASSWORD}"

# Guards the viewer with HTTP basic auth. It defaults to the database password
# only so a run is not blocked on setting it; prefer a separate throwaway,
# because the app is served over plain HTTP and the credential crosses the wire
# in the clear.
APP_PASSWORD="${APP_PASSWORD:-$NEO4J_PASSWORD}"
APP_USER="${APP_USER:-music}"
APP_PORT="${APP_PORT:-80}"

DUMP_YEAR="${DUMP_DATE:0:4}"
DATA_DIR="${DATA_DIR:-/var/lib/music-graph/data}"
CSV_DIR="${CSV_DIR:-$DATA_DIR/csv}"
REPO_DIR="${REPO_DIR:-/opt/music-graph}"
REPO_URL="${REPO_URL:-https://github.com/prehensile/music-graph.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
# Only needed if REPO_URL is private. See fetch_repo() for the caveats.
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
# Community Edition serves exactly one user database, so import into the
# default. Naming it anything else yields a database Neo4j will not start.
DATABASE="${DATABASE:-neo4j}"

# The S3 bucket does not serve objects to anonymous callers -- a direct GET
# returns 403, and because the bucket also denies anonymous listing, S3 answers
# 403 rather than 404 for a key that is simply absent, which makes a wrong path
# look like a permissions problem. Downloads go through the Discogs front end
# instead, which takes the object key url-encoded in a query parameter:
#
#   https://data.discogs.com/?download=data%2F2026%2Fdiscogs_20260801_artists.xml.gz
#
DUMP_HOST="${DUMP_HOST:-https://data.discogs.com/}"
DUMP_PREFIX="${DUMP_PREFIX:-data/${DUMP_YEAR}}"
DUMP_INDEX_URL="https://discogs-data-dumps.s3.us-west-2.amazonaws.com/index.html"

# Build the download URL for one dump file, url-encoding the slashes in the key.
dump_url() {
    printf '%s?download=%s%%2F%s' \
        "$DUMP_HOST" "${DUMP_PREFIX//\//%2F}" "$1"
}

LOG_FILE="${LOG_FILE:-/var/log/music-graph.log}"
STATUS_FILE="${STATUS_FILE:-/var/log/music-graph.status}"
STEPS_FILE="${STEPS_FILE:-/var/log/music-graph.steps.jsonl}"
ENV_FILE=/etc/music-graph.env
UNIT_FILE=/etc/systemd/system/music-graph.service

# Named here so the count is right in the UI before any of them have run.
STEP_NAMES=(
    "Preflight"
    "Installing base packages"
    "Installing Neo4j"
    "Configuring firewall"
    "Fetching repo and dependencies"
    "Starting the viewer service"
    "Downloading Discogs dumps"
    "Transforming dumps to CSV"
    "Importing into Neo4j"
    "Building the search index"
    "Verifying the graph"
)
TOTAL_STEPS=${#STEP_NAMES[@]}
STEP_NUM=0
STEP_NAME=""
STEP_START=0

have() { command -v "$1" >/dev/null 2>&1; }

SELF=$(readlink -f "${BASH_SOURCE[0]}")
script_hash() { sha256sum "$SELF" 2>/dev/null | awk '{print $1}'; }

# Checked before anything opens a file in /var/log, so a non-root run says why
# rather than dying on a permission error.
[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }

# Interactively, tee so progress is visible live. Unattended, redirect straight
# to the file: process substitution can lose buffered output when the shell
# exits, truncating exactly the failure trace worth having.
if [[ -t 1 ]]; then
    exec > >(tee -a "$LOG_FILE") 2>&1
else
    exec >> "$LOG_FILE" 2>&1
fi

log() {
    printf '\n[%s] == %s\n' "$(date -u +%H:%M:%S)" "$*"
    printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" > "$STATUS_FILE"
}

# One JSON object per line, appended as each step starts and ends. The web
# server reads this to render a checklist; a flat file needs no coordination
# between the two processes and survives either being restarted.
emit_step() {
    local state="$1"
    printf '{"n":%d,"of":%d,"name":"%s","state":"%s","elapsed":%d,"ts":"%s"}\n' \
        "$STEP_NUM" "$TOTAL_STEPS" "$STEP_NAME" "$state" \
        "$(( SECONDS - STEP_START ))" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        >> "$STEPS_FILE"
    chmod 644 "$STEPS_FILE" 2>/dev/null || true
}

# Run one named step, bracketing it with progress records.
step() {
    local fn="$1"
    STEP_NUM=$(( STEP_NUM + 1 ))
    STEP_NAME="${STEP_NAMES[$(( STEP_NUM - 1 ))]}"
    STEP_START=$SECONDS
    emit_step "running"
    log "[${STEP_NUM}/${TOTAL_STEPS}] ${STEP_NAME}"
    "$fn"
    emit_step "done"
}

on_error() {
    local rc=$? line=$1
    printf '\n!! FAILED (exit %s) at line %s\n' "$rc" "$line"
    printf 'FAILED (exit %s) at line %s -- see %s\n' "$rc" "$line" "$LOG_FILE" > "$STATUS_FILE"
    [[ -n "$STEP_NAME" ]] && emit_step "failed"
}
trap 'on_error $LINENO' ERR

public_ip() {
    curl -fsS --max-time 5 \
        http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address 2>/dev/null \
        || hostname -I | awk '{print $1}'
}

# ---------------------------------------------------------------- preflight

preflight() {
    mkdir -p "$DATA_DIR" "$CSV_DIR"

    check_dump_urls

    local avail_gb
    avail_gb=$(df -BG --output=avail "$DATA_DIR" | tail -1 | tr -dc '0-9')
    echo "  free space on $DATA_DIR: ${avail_gb} GB"
    # Measured against the 2026-08 dump: 11.5 GB of .gz (releases alone is
    # 10.4 GB), ~1 GB labels staging DB, ~4 GB CSVs, ~15 GB Neo4j store.
    if (( avail_gb < 45 )); then
        echo "  WARNING: under 45 GB free. Peak need is ~32 GB: 11.5 GB of .gz" >&2
        echo "  dumps + ~1 GB labels.db + ~4 GB CSVs + ~15 GB Neo4j store." >&2
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
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq \
        python3 python3-pip python3-venv \
        openjdk-21-jre-headless \
        git curl wget ca-certificates gnupg ufw
    python3 --version

    # The transform uses csv.QUOTE_STRINGS, added in Python 3.12. Ubuntu 24.04
    # ships 3.12, so this should never fire -- but fail now, not hours in.
    python3 - <<'PY'
import csv, sys
if not hasattr(csv, "QUOTE_STRINGS"):
    sys.exit(f"Python {sys.version_info.major}.{sys.version_info.minor} is too old; need 3.12+")
PY
}

install_neo4j() {
    if have neo4j-admin; then
        echo "  already installed"
        return
    fi
    echo "  installing Neo4j 5 Community"
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

configure_firewall() {
    # Neo4j already binds localhost only; this is belt-and-braces so 7474/7687
    # cannot be reached even if that changes.
    ufw --force reset >/dev/null
    ufw default deny incoming >/dev/null
    ufw default allow outgoing >/dev/null
    ufw allow 22/tcp >/dev/null
    ufw allow "${APP_PORT}/tcp" >/dev/null
    ufw --force enable >/dev/null
    ufw status numbered
}

fetch_repo() {
    local self_before
    self_before=$(script_hash)

    # A public repo needs no credentials. For a private one, set GITHUB_TOKEN to
    # a fine-grained token with read-only Contents access to just this repo.
    #
    # The header is passed per command with -c rather than embedded in the
    # remote URL, so it never lands in .git/config on the droplet. It is still
    # visible in the process list while git runs, and DigitalOcean exposes
    # user-data to anything on the box via the metadata service -- so scope the
    # token to one repo, give it the shortest expiry you can, and revoke it once
    # the build is done.
    local git_auth=()
    if [[ -n "$GITHUB_TOKEN" ]]; then
        echo "  using GITHUB_TOKEN for a private clone"
        git_auth=(-c "http.extraheader=Authorization: Basic $(
            printf 'x-access-token:%s' "$GITHUB_TOKEN" | base64 -w0)")
    fi

    if [[ -d "$REPO_DIR/.git" ]]; then
        echo "  updating repo in $REPO_DIR"
        git "${git_auth[@]}" -C "$REPO_DIR" fetch --quiet origin "$REPO_BRANCH"
        git -C "$REPO_DIR" checkout --quiet "$REPO_BRANCH"
        git -C "$REPO_DIR" reset --hard --quiet "origin/$REPO_BRANCH"
    else
        echo "  cloning $REPO_URL"
        if ! git "${git_auth[@]}" clone --quiet --branch "$REPO_BRANCH" \
                "$REPO_URL" "$REPO_DIR"; then
            echo "  clone failed." >&2
            if [[ -z "$GITHUB_TOKEN" ]]; then
                echo "  If $REPO_URL is private, either make it public or set" >&2
                echo "  GITHUB_TOKEN to a read-only fine-grained token." >&2
            else
                echo "  Check the token has Contents:read on this repo and has" >&2
                echo "  not expired, and that the branch name is right." >&2
            fi
            return 1
        fi
    fi
    # bash reads a script incrementally by byte offset, so the pull above can
    # rewrite this file mid-execution and corrupt the rest of the run. If the
    # checkout changed it, restart from the top with the new version; every step
    # is idempotent, so the restart costs only the checks it repeats.
    if [[ "$self_before" != "$(script_hash)" && -z "${MUSIC_GRAPH_REEXEC:-}" ]]; then
        echo "  provisioning script was updated by the pull; restarting with it"
        export MUSIC_GRAPH_REEXEC=1
        exec "$SELF"
    fi

    pip3 install --quiet --break-system-packages -r "$REPO_DIR/requirements.txt"

    log "Vendoring browser libraries"
    "$REPO_DIR/fetch_vendor.sh"
}

# ---------------------------------------------------------------- dumps

check_dump_urls() {
    # Run during preflight so a wrong date or path fails in seconds, rather
    # than several minutes into an unattended provision.
    if ! have curl; then
        echo "  curl not present yet; skipping the URL check"
        return 0
    fi

    # HEAD, not a ranged GET. A server that ignores Range answers a range
    # request with the whole body, which here would mean downloading 10 GB
    # during what is supposed to be a two-second check. HEAD carries no body.
    local name url code bad=0
    echo "  checking dump URLs under ${DUMP_HOST}?download=${DUMP_PREFIX}/"
    for name in artists labels masters releases; do
        url=$(dump_url "discogs_${DUMP_DATE}_${name}.xml.gz")
        code=$(curl -sS --head -o /dev/null -w '%{http_code}' --max-time 30 "$url" 2>/dev/null || echo 000)
        printf '    %-9s HTTP %s\n' "$name" "$code"
        [[ "$code" == 200 ]] || bad=1
    done

    if (( bad )); then
        echo "  Could not reach the dumps for ${DUMP_DATE}." >&2
        echo "  Confirm that date exists at ${DUMP_INDEX_URL}" >&2
        echo "  If the layout has changed, copy a dump's link from that page and" >&2
        echo "  set DUMP_HOST / DUMP_PREFIX to match." >&2
        return 1
    fi
}

# Expected sha256 for one dump file, empty if the checksum file has no line
# for it. Discogs publishes "<sha256> <filename>", one space.
expected_sha() {
    local fname="$1" checksums="$2" hash
    [[ -s "$checksums" ]] || return 0
    hash=$(awk -v want="$fname" 'index($0, want) { print $1; exit }' "$checksums")
    [[ ${#hash} -eq 64 ]] && printf '%s' "$hash"
}

sha_matches() {
    local f="$1" want="$2"
    [[ -n "$want" && -s "$f" ]] || return 1
    [[ "$(sha256sum "$f" | awk '{print $1}')" == "$want" ]]
}

# Cheap discriminator between a genuine partial download and a saved error
# page. Checking the gzip magic costs two bytes; `gzip -t` on the releases dump
# would decompress ~110 GB to tell us the same thing.
looks_like_gzip() {
    [[ -s "$1" ]] || return 1
    [[ "$(head -c 2 "$1" | od -An -tx1 | tr -d ' \n')" == "1f8b" ]]
}

# True if the file matches its published checksum, or -- when no checksum is
# available -- at least decompresses cleanly. The gzip fallback is slow on the
# releases dump (it inflates ~110 GB) but only runs if Discogs stopped
# publishing CHECKSUM.txt.
dump_is_good() {
    local f="$1" want="$2"
    if [[ -n "$want" ]]; then
        sha_matches "$f" "$want"
    else
        gzip -t "$f" 2>/dev/null
    fi
}

# Fetch one dump, resuming across interruptions, and verify it before returning.
#
# wget rather than curl, for a measured reason: curl's own --retry restarts the
# transfer from byte 0 -- every retry goes out with no Range header -- so an
# interrupted 10 GB download is re-fetched from scratch, repeatedly, before any
# outer loop regains control. wget's --tries resumes instead, reissuing with the
# correct Range each time. wget 1.x also speaks only HTTP/1.1, so the Cloudflare
# HTTP/2 stream reset (curl 92) that broke the first run cannot recur.
#
# Verification stays in this loop because neither tool checks content: a
# truncated file can otherwise be reported as a clean download.
fetch_dump() {
    local url="$1" f="$2" want="${3:-}" attempt rc
    for attempt in 1 2 3; do
        # Never resume onto something that is not gzip: an error page saved
        # under the dump's name would otherwise be extended forever.
        if [[ -s "$f" ]] && ! looks_like_gzip "$f"; then
            echo "    discarding non-gzip partial"
            rm -f "$f"
        fi
        if [[ -s "$f" ]]; then
            echo "    attempt ${attempt}, resuming from $(du -h "$f" | cut -f1)"
        else
            echo "    attempt ${attempt}"
        fi

        # --read-timeout abandons a connection stalled mid-body, which is how
        # the Cloudflare failure presented (minutes at 0 B/s before erroring).
        # dot:giga keeps progress to roughly one line per 32 MiB, which is
        # readable in the log rather than thousands of lines.
        rc=0
        wget --continue --tries=10 --waitretry=10 --retry-connrefused \
             --timeout=30 --read-timeout=60 \
             --progress=dot:giga \
             -O "$f" "$url" || rc=$?

        if (( rc == 0 )); then
            if dump_is_good "$f" "$want"; then
                return 0
            fi
            echo "    download verified bad (truncated or corrupt); starting over" >&2
            rm -f "$f"
        else
            echo "    wget gave up (exit ${rc}); retrying" >&2
        fi
        sleep $(( attempt * 20 ))
    done
    return 1
}

download_dumps() {
    local checksums="$DATA_DIR/discogs_${DUMP_DATE}_CHECKSUM.txt"

    if [[ ! -s "$checksums" ]]; then
        wget -q --tries=5 --timeout=30 \
            -O "$checksums" "$(dump_url "discogs_${DUMP_DATE}_CHECKSUM.txt")" \
            || echo "  no CHECKSUM file retrieved; downloads cannot be verified"
    fi

    local name fname f want
    for name in artists labels masters releases; do
        fname="discogs_${DUMP_DATE}_${name}.xml.gz"
        f="$DATA_DIR/$fname"
        want=$(expected_sha "$fname" "$checksums")

        if [[ -f "$f.ok" ]]; then
            echo "  $fname verified previously, skipping"
            continue
        fi

        # A complete file left by a run that died before verifying costs one
        # checksum to recognise, which beats fetching 10 GB again.
        if sha_matches "$f" "$want"; then
            echo "  $fname already complete"
            touch "$f.ok"
            continue
        fi

        echo "  fetching $fname"
        fetch_dump "$(dump_url "$fname")" "$f" "$want" || {
            echo "  giving up on $fname after 6 attempts" >&2
            if looks_like_gzip "$f"; then
                echo "  keeping the $(du -h "$f" | cut -f1) partial -- re-running resumes it" >&2
            else
                rm -f "$f"
            fi
            exit 1
        }

        # fetch_dump only returns 0 once the file has passed verification.
        echo "  $fname verified"
        touch "$f.ok"
    done

    du -sh "$DATA_DIR"
}

# ---------------------------------------------------------------- transform

run_transform() {
    if [[ -s "$CSV_DIR/releases.csv" && -s "$CSV_DIR/labels.csv" ]]; then
        log "CSVs already present in $CSV_DIR, skipping transform"
        return
    fi

    # The transform refuses a non-empty output folder, since its writers append.
    rm -f "$CSV_DIR"/*.csv "$DATA_DIR/labels.db"

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
        if curl -fsS -o /dev/null "http://localhost:7474/" 2>/dev/null; then
            echo "  up after $((i*10))s"; return 0
        fi
        sleep 10
    done
    echo "  Neo4j did not answer within 10 minutes -- journalctl -u neo4j -n 50" >&2
    return 1
}

create_indexes() {
    export NEO4J_USERNAME=neo4j NEO4J_PASSWORD
    # The viewer searches through this index. A CONTAINS scan over ~15M nodes
    # would not return in usable time.
    cypher-shell -d "$DATABASE" \
        "CREATE FULLTEXT INDEX entitySearch IF NOT EXISTS
         FOR (n:Artist|Group|Release|Label) ON EACH [n.name, n.title];"
    echo "  waiting for the index to come online (this can take a while)"
    cypher-shell -d "$DATABASE" "CALL db.awaitIndexes(1800);"
    cypher-shell -d "$DATABASE" "SHOW INDEXES YIELD name, state, type WHERE type = 'FULLTEXT';"
}

# ---------------------------------------------------------------- the app

install_app() {

    # Credentials live in a 600 env file rather than the unit, which is
    # world-readable by default.
    cat > "$ENV_FILE" <<EOF
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=${NEO4J_PASSWORD}
NEO4J_DATABASE=${DATABASE}
APP_USER=${APP_USER}
APP_PASSWORD=${APP_PASSWORD}
APP_PORT=${APP_PORT}
APP_BIND=0.0.0.0
LOG_FILE=${LOG_FILE}
STATUS_FILE=${STATUS_FILE}
STEPS_FILE=${STEPS_FILE}
EOF
    chmod 600 "$ENV_FILE"

    # The service runs as neo4j and reads these to render /logs.
    touch "$LOG_FILE" "$STATUS_FILE" "$STEPS_FILE"
    chmod 644 "$LOG_FILE" "$STATUS_FILE" "$STEPS_FILE"

    cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Music Graph viewer
# After, but deliberately not Wants: the service comes up before the import and
# must not drag Neo4j up with it, since neo4j-admin needs Neo4j stopped. The
# viewer tolerates the database being absent and says so on /logs.
After=neo4j.service

[Service]
Type=simple
User=neo4j
Group=neo4j
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/python3 ${REPO_DIR}/server.py
Restart=on-failure
RestartSec=5
# Lets an unprivileged service bind port 80 without running as root.
AmbientCapabilities=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable music-graph >/dev/null 2>&1 || true
    systemctl restart music-graph

    # Probe /api/progress, not /api/stats: this runs before the import, so the
    # graph endpoints cannot succeed yet. /api/progress only reads log files.
    local i
    for i in $(seq 1 20); do
        if curl -fsS -o /dev/null -u "${APP_USER}:${APP_PASSWORD}" \
                "http://localhost:${APP_PORT}/api/progress" 2>/dev/null; then
            echo "  viewer responding after ${i}s"
            echo "  progress page: http://$(public_ip)/logs"
            return 0
        fi
        sleep 1
    done
    echo "  viewer did not answer -- journalctl -u music-graph -n 50" >&2
    return 1
}

verify() {
    export NEO4J_USERNAME=neo4j NEO4J_PASSWORD
    cypher-shell -d "$DATABASE" \
        "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS nodes ORDER BY nodes DESC;"
    cypher-shell -d "$DATABASE" \
        "MATCH ()-[r]->() RETURN type(r) AS rel, count(*) AS count ORDER BY count DESC;"
}

# ---------------------------------------------------------------- main

main() {
    local started=$SECONDS
    : > "$STEPS_FILE"
    chmod 644 "$STEPS_FILE" "$LOG_FILE" "$STATUS_FILE" 2>/dev/null || true

    # Seed a pending record for every step so the progress page can show the
    # whole checklist by name from the outset, rather than the list growing as
    # the run proceeds. Later records for a step supersede these.
    local i
    for i in "${!STEP_NAMES[@]}"; do
        STEP_NUM=$(( i + 1 ))
        STEP_NAME="${STEP_NAMES[$i]}"
        STEP_START=$SECONDS
        emit_step "pending"
    done
    STEP_NUM=0
    STEP_NAME=""

    step preflight
    step install_packages
    step install_neo4j
    step configure_firewall
    step fetch_repo
    # Deliberately before the slow steps: this brings up /logs, so the rest of
    # the run can be watched from a browser. The graph endpoints stay broken
    # until the import lands, which the progress page says plainly.
    step install_app
    step download_dumps
    step run_transform
    step import_graph
    step create_indexes
    step verify

    # The stats endpoint caches its counts, and the service has been up since
    # before the import. Restart so it reports the populated graph.
    systemctl restart music-graph

    local mins=$(( (SECONDS - started) / 60 ))
    local ip; ip=$(public_ip)
    local url="http://${ip}"
    [[ "$APP_PORT" == "80" ]] || url="http://${ip}:${APP_PORT}"

    printf '\n[%s] == Done in %s minutes\n' "$(date -u +%H:%M:%S)" "$mins"
    printf 'DONE in %s minutes -- %s\n' "$mins" "$url" > "$STATUS_FILE"

    cat <<EOF

    Viewer:   ${url}
    Sign in:  ${APP_USER} / the APP_PASSWORD you set

Neo4j itself stays on localhost and is not reachable from outside; only the
viewer port is open. The viewer is plain HTTP, so treat that password as
throwaway and do not reuse it.

Logs:   ${LOG_FILE}
Status: ${STATUS_FILE}

When you are finished, snapshot or destroy the droplet -- it bills by the hour.
EOF
}

main "$@"
