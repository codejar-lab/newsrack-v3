# Local build -> GitHub Pages

A container builds the site daily at **06:00 IST** (supercronic, `TZ=Asia/Kolkata`)
and publishes it to the existing Pages site instead of the GitHub runner.

```
container: build.sh -> public/ ──force-push──> branch `pages-payload`
                                  └─ workflow_dispatch ─> deploy-payload.yml (on main) ─> Pages
```

## One-time setup
1. Merge `.github/workflows/deploy-payload.yml` to `main` (dispatch needs it on the default branch).
   `build.yml` still builds on pushes to `main`; switch it to `workflow_dispatch` only to stop runner builds.
2. Create a fine-grained PAT for this repo: **Contents: RW**, **Actions: RW**.
3. `cp local/.env.example local/.env` and set `GH_TOKEN`.

## Run
```bash
podman build -f local/Containerfile --build-arg GIT_SHA=$(git rev-parse HEAD) -t newsrack-local .
podman run -d --name newsrack --restart=always --network=pasta:-4 --env-file local/.env \
  -v newsrack-meta:/app/meta newsrack-local
podman exec newsrack /app/local/run-and-publish.sh     # publish now
podman exec -e DRY_RUN=1 newsrack /app/local/run-and-publish.sh   # build only
podman logs -f newsrack
```
Rebuild the image to pick up code changes. The `newsrack-meta` volume keeps the
newsletter de-dup state between runs. For restart-on-boot, use
`podman generate systemd`/a Quadlet (the host must be on at 06:00).

## Why `--network=pasta:-4`
Inside the rootless container every IPv6 connection times out (v4 is fine), and
Python/calibre try IPv6 first, so each fetch burned its full timeout and retried.
A normal 1-minute build then ran past calibre's 20-minute limit and produced no
EPUB. Keep the container IPv4-only.

## Failure behaviour
Each recipe is limited to 5 minutes (`RECIPE_TIMEOUT_SECONDS` in `_recipes_custom.py`, no inner retry) and the whole build to `BUILD_TIMEOUT` (600 s). On a timeout the entire process tree is killed and the build restarts from scratch. `run-and-publish.sh` retries the build up to 3 times (`BUILD_ATTEMPTS`, `RETRY_WAIT`) and publishes only if
today's `india-opinion-<date>.epub` exists. Otherwise the live site is left
untouched and a high-priority ntfy alert goes to `$NTFY_TOPIC` (default `sdm_dhari`).
