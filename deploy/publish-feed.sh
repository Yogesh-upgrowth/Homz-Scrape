#!/usr/bin/env bash
# Regenerates the website feed and pushes it, which is what actually makes
# the site update — Vercel deploys `api/data.js` + `data/feed/*.json` from
# git, not from whatever's sitting on this host's disk. `export feed` alone
# only updates the local files; without the commit+push here, the cron job
# would run daily and nothing on the live site would ever change.
#
# Requires this host to have push access to the tracked branch already
# configured (SSH deploy key or a PAT in the remote URL) — set up once,
# outside this script.
set -euo pipefail

cd "${HOMZ_HOME:?HOMZ_HOME must be set}"

"${HOMZ_BIN:?HOMZ_BIN must be set}" export feed --out "$HOMZ_HOME/data/feed"

if git diff --quiet -- data/feed && git diff --cached --quiet -- data/feed; then
    echo "publish-feed: no change in data/feed, nothing to push"
    exit 0
fi

git add data/feed
git commit -m "chore: refresh feed $(date -u +%Y-%m-%dT%H:%M:%SZ)" -q
git push origin "$(git rev-parse --abbrev-ref HEAD)"
echo "publish-feed: pushed updated feed, Vercel will redeploy from git"
