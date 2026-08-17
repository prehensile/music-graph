#!/bin/bash
# Paste this into the "User data" box when creating a DigitalOcean droplet
# (Advanced Options -> Add Initialization scripts). Nothing else is needed --
# no SSH, no terminal. The droplet provisions and runs the whole import on
# first boot, then notifies you.
#
# Fill in the four values below first.
#
# Droplet: Ubuntu 24.04, s-4vcpu-8gb (8 GB / 4 vCPU). ~4-6 hours, well under
# a pound if you destroy it afterwards.

export DUMP_DATE=20250801                      # check the date exists first
export NEO4J_PASSWORD='change-me-please'       # your Neo4j login
export NTFY_TOPIC='music-graph-pick-something-unguessable'
export TAILSCALE_AUTHKEY=''                    # optional; see below

# NTFY_TOPIC: install the ntfy app on your phone and subscribe to this exact
# topic to get a push at every stage, and a loud one if anything fails. Topics
# are public to anyone who guesses the name, so make it long and random.
#
# TAILSCALE_AUTHKEY: optional but recommended on a phone. Generate one at
# https://login.tailscale.com/admin/settings/keys . With it, Neo4j Browser
# becomes reachable at http://<tailnet-ip>:7474 from your phone with no ports
# open to the internet, and the Tailscale app also gives you SSH without keys.
# Leave it empty to keep Neo4j on localhost only.

BRANCH=claude/extract-archive-update-docs-c14mck

apt-get update -qq
apt-get install -y -qq git
git clone --quiet --branch "$BRANCH" \
    https://github.com/prehensile/music-graph.git /opt/music-graph
exec /opt/music-graph/provision_droplet.sh
