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

ntfy_alert() {  # best-effort push; never fails the script
  curl -fsS -m 20 -H "Title: Daily Digest build FAILED" -H "Priority: high" -H "Tags: rotating_light" \
    -d "$1" "${NTFY_SERVER:-https://ntfy.sh}/${NTFY_TOPIC:-sdm_dhari}" >/dev/null 2>&1 || true
}

# A recipe that times out still lets build.sh exit 0 and leaves public/ without
# its EPUB; publishing that would REPLACE the live site with an empty one.
# So: retry the whole build, and only publish if the book exists.
ATTEMPTS="${BUILD_ATTEMPTS:-3}"
# Per-recipe limit is 5 min (_recipes_custom.py); this outer limit also covers
# the static-asset/minify steps. On expiry the whole process tree is killed and
# the build restarts from scratch.
BUILD_TIMEOUT="${BUILD_TIMEOUT:-600}"
ok=0
for i in $(seq 1 "$ATTEMPTS"); do
  echo "=== $(date -Is) build start (attempt $i/$ATTEMPTS) ==="
  rm -rf public output
  rm -rf *.recipe
  # own session/process group, so one kill takes down build.sh, _generate.py
  # and every ebook-convert it spawned
  setsid sh build.sh & bp=$!
  ( sleep "$BUILD_TIMEOUT"; echo "!! build exceeded ${BUILD_TIMEOUT}s, killing it"; kill -KILL -- "-$bp" 2>/dev/null ) & wd=$!
  wait "$bp" || echo "build.sh exited non-zero"
  kill "$wd" 2>/dev/null || true
  kill -KILL -- "-$bp" 2>/dev/null || true   # nothing may outlive an attempt
  [ -f job_summary.md ] && cat job_summary.md
  if ls "public/india-opinion-$(date +%F).epub" >/dev/null 2>&1; then ok=1; break; fi
  echo "!! no india-opinion EPUB produced on attempt $i"
  [ "$i" -lt "$ATTEMPTS" ] && sleep "${RETRY_WAIT:-60}"
done
rm -rf "$CALIBRE_CONFIG_DIRECTORY"
if [ "$ok" != 1 ]; then
  echo "=== build failed after $ATTEMPTS attempts; NOT publishing (live site left untouched) ==="
  ntfy_alert "No EPUB after $ATTEMPTS attempts at $(date +%H:%M). Live site left unchanged. Check: podman logs newsrack"
  exit 1
fi
echo -e "\n<"'!'"-- local build $(date -Is), $(ebook-convert --version | head -n1) -->" >> public/index.html

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
