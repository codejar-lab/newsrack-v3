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

ntfy_alert() {  # best-effort push; never fails the script. $1 msg [$2 title] [$3 priority] [$4 tags]
  curl -fsS -m 20 -H "Title: ${2:-Daily Digest build FAILED}" -H "Priority: ${3:-high}" \
    -H "Tags: ${4:-rotating_light}" \
    -d "$1" "${NTFY_SERVER:-https://ntfy.sh}/${NTFY_TOPIC:-sdm_dhari}" >/dev/null 2>&1 || true
}

# Inspect a built Daily Digest EPUB. Exit 1 if it is unusable (missing, or the
# e-ink optimizer didn't run -- _generate.py swallows optimizer errors); else
# print one line per gap worth warning about (missing main source).
check_book() {
  python3 -I - "$1" <<'PY'
import re, sys, zipfile
try:
    z = zipfile.ZipFile(sys.argv[1])
except Exception as e:
    print("unreadable EPUB: %s" % e); sys.exit(1)
css = "".join(z.read(n).decode("utf-8", "replace") for n in z.namelist() if n.endswith(".css"))
if not re.search(r"/\*eink-base\d+\*/", css):
    print("EPUB is not e-ink optimized"); sys.exit(1)
ncx = "".join(z.read(n).decode("utf-8", "replace") for n in z.namelist() if n.endswith(".ncx"))
labels = re.findall(r"<text>(.*?)</text>", ncx, re.S)
for prefix, name in (("Indian Express:", "Indian Express"), ("The Hindu:", "The Hindu"), ("NL: ", "newsletters")):
    if not any(l.strip().startswith(prefix) for l in labels):
        print("no %s section" % name)
PY
}

# newsletter de-dup state is staged as *.pending by the recipe and only
# committed once the book is actually live (see daily_digest._save_seen_urls)
seen_pending() { ls meta/daily_digest_seen_urls__*.json.pending 2>/dev/null || true; }

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
  # the watchdog kills its own sleep when stopped, so nothing lingers after a
  # good build (otherwise supercronic logs "reaper cleanup" 10 min later)
  ( trap 'kill $s 2>/dev/null; exit 0' TERM
    sleep "$BUILD_TIMEOUT" & s=$!; wait $s
    echo "!! build exceeded ${BUILD_TIMEOUT}s, killing it"; kill -KILL -- "-$bp" 2>/dev/null ) & wd=$!
  wait "$bp" || echo "build.sh exited non-zero"
  kill "$wd" 2>/dev/null || true
  kill -KILL -- "-$bp" 2>/dev/null || true   # nothing may outlive an attempt
  [ -f job_summary.md ] && cat job_summary.md
  book="public/india-opinion-$(date +%F).epub"
  if [ -f "$book" ] && gaps="$(check_book "$book")"; then ok=1; break; fi
  echo "!! no usable india-opinion EPUB on attempt $i${gaps:+: $gaps}"
  gaps=""
  [ "$i" -lt "$ATTEMPTS" ] && sleep "${RETRY_WAIT:-60}"
done
rm -rf "$CALIBRE_CONFIG_DIRECTORY"
if [ "$ok" != 1 ]; then
  echo "=== build failed after $ATTEMPTS attempts; NOT publishing (live site left untouched) ==="
  ntfy_alert "No EPUB after $ATTEMPTS attempts at $(date +%H:%M). Live site left unchanged. Check: podman logs newsrack"
  rm -f $(seen_pending)
  exit 1
fi
[ -f "public/inshorts-$(date +%F).epub" ] || gaps="${gaps:+$gaps
}no Inshorts EPUB"
echo -e "\n<"'!'"-- local build $(date -Is), $(ebook-convert --version | head -n1) -->" >> public/index.html

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "DRY_RUN=1: built public/ ($(du -sh public | cut -f1)), not publishing"
  [ -n "$gaps" ] && echo "gaps: $gaps"
  rm -f $(seen_pending)
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
for f in $(seen_pending); do mv -f "$f" "${f%.pending}"; done
if [ -n "$gaps" ]; then
  echo "=== published with gaps: $gaps ==="
  ntfy_alert "Published at $(date +%H:%M), but: $gaps" "Daily Digest built with gaps" default warning
fi
echo "=== $(date -Is) done: ${CI_PAGES_URL}/ ==="
