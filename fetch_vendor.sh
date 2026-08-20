#!/usr/bin/env bash
#
# Download the two browser libraries the viewer needs into web/vendor/.
#
# They are vendored rather than pulled from a CDN at page load so the app keeps
# working on flaky mobile data and does not depend on unpkg staying up. Run once
# after cloning; provision_droplet.sh calls it automatically.
#
set -euo pipefail

VENDOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/web/vendor"
mkdir -p "$VENDOR_DIR"

fetch() {
    local url="$1" out="$VENDOR_DIR/$2"
    if [[ -s "$out" ]]; then
        echo "  have $2"
        return
    fi
    echo "  fetching $2"
    curl -fsSL --retry 4 --retry-delay 3 "$url" -o "$out" || {
        echo "  FAILED: $url" >&2
        rm -f "$out"
        exit 1
    }
    # Guard against a proxy or error page being saved as if it were the library.
    if [[ $(wc -c < "$out") -lt 10000 ]]; then
        echo "  suspiciously small download for $2 -- refusing it" >&2
        rm -f "$out"
        exit 1
    fi
}

echo "Vendoring browser libraries into $VENDOR_DIR"
fetch "https://unpkg.com/graphology@0.25.4/dist/graphology.umd.min.js" "graphology.umd.min.js"
# sigma 3's UMD bundle moved from build/ to dist/, and bundles createNodeBorderProgram
# (formerly the separate @sigma/node-border package, which needs a bundler -- it has
# no UMD build of its own) directly into window.Sigma.rendering -- see app.js.
fetch "https://unpkg.com/sigma@3.0.3/dist/sigma.min.js"                "sigma.min.js"
echo "Done."
