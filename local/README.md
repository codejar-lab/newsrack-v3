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
podman run -d --name newsrack --restart=always --env-file local/.env \
  -v newsrack-meta:/app/meta newsrack-local
podman exec newsrack /app/local/run-and-publish.sh     # publish now
podman exec -e DRY_RUN=1 newsrack /app/local/run-and-publish.sh   # build only
podman logs -f newsrack
```
Rebuild the image to pick up code changes. The `newsrack-meta` volume keeps the
newsletter de-dup state between runs. For restart-on-boot, use
`podman generate systemd`/a Quadlet (the host must be on at 06:00).
