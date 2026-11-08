#!/bin/sh
# Compile Pocket Shell (launcher) and applets for the T-Deck host contract
# (../tdeck/pocket.host.json), in Docker so untrusted dependencies never
# install on the host.
#   SHELL_SRC  pocket-shell checkout with submodules (vendor/pocketjs, vendor/applets)
#   APPS       space separated: "shell" or an applet dir of vendor/applets
#              (default: "shell notes")
# Output: $OUT/<name>.pocket   (shell -> pocket-shell.pocket)
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
: "${SHELL_SRC:?set SHELL_SRC}"
OUT=${OUT:-$HERE/out}; mkdir -p "$OUT"
APPS=${APPS:-"shell notes"}
docker run --rm -e APPS="$APPS" -v "$SHELL_SRC":/shell -v "$HERE/../tdeck":/host -v "$OUT":/out espressif/idf:v6.0.1 bash -c '
export PATH=/root/.bun/bin:$PATH
if ! command -v bun >/dev/null; then curl -fsSL https://bun.sh/install | bash -s "bun-v1.4.2" >/dev/null 2>&1; fi
cd /shell && bun install --frozen-lockfile >/dev/null && bun install --cwd vendor/pocketjs --frozen-lockfile >/dev/null && bun scripts/setup.ts
for app in $APPS; do
  if [ "$app" = shell ]; then
    manifest=/shell/shells/3ds/shell/pocket.app.json; root=/shell/shells/3ds; out=pocket-shell
  else
    manifest=/shell/vendor/applets/$app/pocket.app.json; root=/shell/vendor/applets; out=$app
  fi
  (cd $root && POCKETJS_FRAMEWORK_ROOT=/shell/vendor/pocketjs bun /shell/vendor/pocketjs/tools/cli/bin.mjs compile \
    --manifest $manifest --project-root $root --host-config /host/pocket.host.config.ts \
    --outdir /out/build-$app --plan-dir /out/plan-$app --output /out/$out.pocket 2>&1 | tail -n 4)
done
ls -la /out/*.pocket
'
