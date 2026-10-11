#!/bin/bash
# Build the periodicals exactly like CI, then push the site to GitHub Pages.
#   GH_TOKEN   fine-grained PAT (contents:write + actions:write) for $GITHUB_REPOSITORY
#   DRY_RUN=1  build only, don't publish
set -euo pipefail
cd /app

export GITHUB_REPOSITORY="${GITHUB_REPOSITORY:-codejar-lab/newsrack-v3}"
export GITHUB_SERVER_URL="${GITHUB_SERVER_URL:-https://github.com}"
export CI_PAGES_URL="${CI_PAGES_URL:-https://${GITHUB_REPOSITORY%%/*}.github.io/${GITHUB_REPOSITORY##*/}}"
export GITHUB_RUN_ID="$(date +%s)"
export xteink_optimize=true
export CALIBRE_CONFIG_DIRECTORY="$(mktemp -d)/calibre_config/"
export PAYLOAD_BRANCH="${PAYLOAD_BRANCH:-pages-payload}"

echo "=== $(date -Is) build start ==="
rm -rf public output
# the Pages comment line CI appends to index.html
sh build.sh
[ -f job_summary.md ] && cat job_summary.md
echo -e "\n<"'!'"-- local build $(date -Is), $(ebook-convert --version | head -n1) -->" >> public/index.html
rm -rf "$CALIBRE_CONFIG_DIRECTORY"

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "DRY_RUN=1: built public/ ($(du -sh public | cut -f1)), not publishing"
  exit 0
fi
: "${GH_TOKEN:?GH_TOKEN is required to publish}"

echo "=== publishing public/ -> ${PAYLOAD_BRANCH} ==="
cd public
rm -rf .git
git init -q -b "$PAYLOAD_BRANCH"
git config user.name  "newsrack-local"
git config user.email "newsrack-local@users.noreply.github.com"
git add -A
git commit -q -m "Pages payload $(date -Is)"
# single orphan commit, force-pushed: the branch never accumulates history
git push -q --force \
  "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git" \
  "$PAYLOAD_BRANCH"
cd ..

echo "=== triggering Pages deploy ==="
curl -fsS -X POST \
  -H "Authorization: Bearer ${GH_TOKEN}" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/${GITHUB_REPOSITORY}/actions/workflows/deploy-payload.yml/dispatches" \
  -d "{\"ref\":\"main\",\"inputs\":{\"branch\":\"${PAYLOAD_BRANCH}\"}}"
echo "=== $(date -Is) done: ${CI_PAGES_URL}/ ==="
