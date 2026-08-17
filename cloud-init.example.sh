#!/bin/bash
# Paste this into the "User data" box when creating a DigitalOcean droplet
# (Advanced Options -> Add Initialization scripts). Nothing else is needed --
# no SSH, no terminal. The droplet provisions, imports and starts serving the
# viewer on first boot.
#
# Fill in the three values below first.
#
# Droplet: Ubuntu 24.04, s-4vcpu-8gb (8 GB / 4 vCPU). ~4-6 hours, well under
# a pound if you destroy it afterwards.
#
# When it finishes, the viewer is at http://<droplet-ip>/ -- sign in with
# APP_USER and APP_PASSWORD. Progress meanwhile:
#   /var/log/music-graph.status   one line, current stage
#   /var/log/music-graph.log      full log

export DUMP_DATE=20250801                    # check this date exists first
export NEO4J_PASSWORD='change-me-please'     # database password
export APP_PASSWORD='change-me-too'          # viewer login; sent over plain
                                             # HTTP, so use a throwaway

BRANCH=claude/extract-archive-update-docs-c14mck

apt-get update -qq
apt-get install -y -qq git
git clone --quiet --branch "$BRANCH" \
    https://github.com/prehensile/music-graph.git /opt/music-graph
exec /opt/music-graph/provision_droplet.sh
