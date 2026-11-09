#!/usr/bin/env bash
#
# Build the vtOS firmware images in one run and publish them as a GitHub release.
#
#   tools/release.sh v0.1.15                 build, package, confirm, publish
#   tools/release.sh v0.1.15 --dry-run       build and package only (nothing is tagged or uploaded)
#   tools/release.sh v0.1.15 --from DIR      reuse firmware.bin / micropython.bin from DIR (skip the build)
#
# Needs: docker (the Makefile build), gh (logged in), git, zip. The first build
# needs `make init` once so the MicroPython source volume exists.
#
# What it produces in release/<tag>/ and uploads:
#   vtOS-<tag>.bin           single-image firmware, flash at 0x0 (what the README tells users to flash)
#   vtOS-<tag>-dualboot.bin  merged dual-boot image (bootloader, partition table, loader menu, vtOS), flash at 0x0
#   vtOS-<tag>-slot.bin      vtOS alone, flash at 0x110000 on a device that is already dual-boot (keeps /flash)
#   vtOS-<tag>.zip           all of the above plus bootloader.bin, partition-table.bin, TAILSCALE_NOTICES.md
#   SHA256SUMS.txt
#
# The release is created from the commit you are on, so it must be committed and
# pushed. The tag is created on GitHub by `gh release create`.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BANNER_FILE="modules/scripts/sys/shell.py"
LOADER_DIR="utils/dualboot/loader"
IDF_IMAGE="${IDF_IMAGE:-espressif/idf:v5.5.1}"
MPY_VOLUME="${MPY_VOLUME:-micropython_src_vol}"
BOARD="LILYGO_T_DECK"

TAG=""
DRY_RUN=0
FROM_DIR=""
DUALBOOT=1
REBUILD_LOADER=0
DRAFT=0
PRERELEASE=0
NOTES_FILE=""
REPO=""
ALLOW_DIRTY=0
ALLOW_CI=0
ASSUME_YES=0

usage() {
  sed -n '3,22p' "$0" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options:
  --dry-run          build and package, but do not tag or publish
  --from DIR         use firmware.bin + micropython.bin (+ bootloader.bin, partition-table.bin) from DIR
  --no-dualboot      skip the merged dual-boot image
  --rebuild-loader   rebuild the dual-boot loader even if a build exists
  --draft            create the release as a draft
  --prerelease       mark the release as a pre-release
  --notes-file FILE  your own notes, placed above the generated ones
  --repo OWNER/NAME  GitHub repository (default: taken from the origin remote)
  --allow-dirty      skip the "everything is committed" check
  --allow-ci         publish even though pushing a v* tag also starts the CI release workflow
  -y, --yes          do not ask for confirmation
  -h, --help         this text
EOF
}

info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --from) [ $# -ge 2 ] || die "--from needs a directory"; FROM_DIR="$2"; shift ;;
    --no-dualboot) DUALBOOT=0 ;;
    --rebuild-loader) REBUILD_LOADER=1 ;;
    --draft) DRAFT=1 ;;
    --prerelease) PRERELEASE=1 ;;
    --notes-file) [ $# -ge 2 ] || die "--notes-file needs a file"; NOTES_FILE="$2"; shift ;;
    --repo) [ $# -ge 2 ] || die "--repo needs OWNER/NAME"; REPO="$2"; shift ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --allow-ci) ALLOW_CI=1 ;;
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown option: $1 (try --help)" ;;
    *) [ -z "$TAG" ] || die "only one tag allowed (got '$TAG' and '$1')"; TAG="$1" ;;
  esac
  shift
done

[ -n "$TAG" ] || { usage; exit 1; }
printf '%s' "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.]+)?$' \
  || die "tag must look like v1.2.3 or v1.2.3-rc1 (got '$TAG')"

sha256() { if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$@"; else sha256sum "$@"; fi; }

# --- preflight --------------------------------------------------------------

info "Preflight checks"
for c in git gh zip; do command -v "$c" >/dev/null 2>&1 || die "'$c' is not installed"; done
if [ -z "$FROM_DIR" ] || [ "$DUALBOOT" = 1 ]; then
  command -v docker >/dev/null 2>&1 || die "'docker' is not installed"
  docker info >/dev/null 2>&1 || die "the docker daemon is not running"
fi
if [ -z "$FROM_DIR" ]; then
  docker volume inspect "$MPY_VOLUME" >/dev/null 2>&1 \
    || die "docker volume '$MPY_VOLUME' is missing; run 'make init' once to fetch MicroPython"
fi
gh auth status >/dev/null 2>&1 || die "gh is not logged in; run 'gh auth login'"

if [ -z "$REPO" ]; then
  url="$(git remote get-url origin 2>/dev/null || true)"
  REPO="$(printf '%s' "$url" | sed -E 's#^(git@github\.com:|https://github\.com/|ssh://git@github\.com/)##; s#\.git$##')"
  printf '%s' "$REPO" | grep -Eq '^[^/]+/[^/]+$' || die "could not work out the GitHub repo from origin ('$url'); pass --repo OWNER/NAME"
fi

SHA="$(git rev-parse HEAD)"
SHORT="$(git rev-parse --short HEAD)"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

if [ -n "$(git status --porcelain)" ]; then
  if [ "$ALLOW_DIRTY" = 1 ]; then
    warn "working tree has uncommitted changes; this image will not match commit $SHORT"
  else
    git status --short | head -15 >&2
    die "uncommitted changes. Commit them first (the release must match a commit), or pass --allow-dirty"
  fi
fi

git fetch --quiet origin 2>/dev/null || warn "could not fetch origin; the 'is it pushed' check may be stale"
if ! git branch -r --contains "$SHA" 2>/dev/null | grep -q .; then
  if [ "$DRY_RUN" = 1 ]; then
    warn "commit $SHORT is not on any remote branch yet (push it before a real release)"
  else
    die "commit $SHORT is not pushed. Run 'git push origin $BRANCH' first"
  fi
fi

if git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1 \
   || git rev-parse -q --verify "refs/tags/$TAG" >/dev/null 2>&1; then
  die "tag $TAG already exists"
fi
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
  die "a release for $TAG already exists on $REPO"
fi

if grep -Eq '^[[:space:]]*tags:' .github/workflows/release.yml 2>/dev/null; then
  msg="pushing a v* tag also starts .github/workflows/release.yml, which builds on GitHub and uploads vtOS-$TAG.bin over the one built here"
  if [ "$ALLOW_CI" = 1 ] || [ "$DRY_RUN" = 1 ]; then
    warn "$msg"
  else
    die "$msg. Limit that workflow to workflow_dispatch, or pass --allow-ci to accept it"
  fi
fi

OUT="$REPO_ROOT/release/$TAG"
rm -rf "$OUT"
mkdir -p "$OUT/build"

# --- build ------------------------------------------------------------------

BANNER_BACKUP=""
restore_banner() {
  if [ -n "$BANNER_BACKUP" ] && [ -f "$BANNER_BACKUP" ]; then
    cp "$BANNER_BACKUP" "$BANNER_FILE"
    rm -f "$BANNER_BACKUP"
  fi
}
trap restore_banner EXIT INT TERM

if [ -n "$FROM_DIR" ]; then
  info "Using prebuilt images from $FROM_DIR (no build)"
  for f in firmware.bin micropython.bin; do
    [ -f "$FROM_DIR/$f" ] || die "$FROM_DIR/$f not found"
    cp "$FROM_DIR/$f" "$OUT/build/$f"
  done
  for f in bootloader.bin partition-table.bin TAILSCALE_NOTICES.md; do
    [ -f "$FROM_DIR/$f" ] && cp "$FROM_DIR/$f" "$OUT/build/$f"
  done
else
  info "Stamping the shell banner with $TAG (restored afterwards)"
  BANNER_BACKUP="$(mktemp)"
  cp "$BANNER_FILE" "$BANNER_BACKUP"
  sed -E -i.sedbak "s/(vtOS )v[0-9A-Za-z.+-]*(\\.\\\\n)/\\1$TAG\\2/" "$BANNER_FILE"
  rm -f "$BANNER_FILE.sedbak"
  grep -q "vtOS $TAG\\." "$BANNER_FILE" || die "could not stamp the version into $BANNER_FILE"

  info "Building firmware (docker, several minutes)"
  make build BUILD_DIR="$OUT/build"
  restore_banner
  BANNER_BACKUP=""

  info "Collecting bootloader and partition table from the build volume"
  docker run --rm -v "$MPY_VOLUME":/opt/micropython -v "$OUT/build":/out alpine sh -c \
    "cp /opt/micropython/ports/esp32/build-$BOARD/bootloader/bootloader.bin \
        /opt/micropython/ports/esp32/build-$BOARD/partition_table/partition-table.bin /out/"
fi

[ -s "$OUT/build/firmware.bin" ] || die "firmware.bin is missing or empty"
[ -s "$OUT/build/micropython.bin" ] || die "micropython.bin is missing or empty"

# --- dual-boot merged image -------------------------------------------------

LB="$REPO_ROOT/$LOADER_DIR/build"
if [ "$DUALBOOT" = 1 ]; then
  if [ "$REBUILD_LOADER" = 1 ] || [ ! -f "$LB/dualboot_loader.bin" ]; then
    info "Building the dual-boot loader (docker, $IDF_IMAGE)"
    docker run --rm -v "$REPO_ROOT/$LOADER_DIR":/work -w /work "$IDF_IMAGE" idf.py build
  fi
  for f in bootloader/bootloader.bin partition_table/partition-table.bin ota_data_initial.bin dualboot_loader.bin; do
    [ -f "$LB/$f" ] || die "loader build is missing $f"
  done
  info "Merging the dual-boot image"
  docker run --rm -v "$OUT":/out -v "$LB":/loader "$IDF_IMAGE" \
    esptool.py --chip esp32s3 merge_bin -o "/out/vtOS-$TAG-dualboot.bin" \
      --flash_mode dio --flash_freq 80m --flash_size 16MB \
      0x0 /loader/bootloader/bootloader.bin \
      0x8000 /loader/partition_table/partition-table.bin \
      0xd000 /loader/ota_data_initial.bin \
      0x10000 /loader/dualboot_loader.bin \
      0x110000 /out/build/micropython.bin >/dev/null
  [ -s "$OUT/vtOS-$TAG-dualboot.bin" ] || die "dual-boot merge produced nothing"
fi

# --- package ----------------------------------------------------------------

info "Packaging"
cp "$OUT/build/firmware.bin" "$OUT/vtOS-$TAG.bin"
cp "$OUT/build/micropython.bin" "$OUT/vtOS-$TAG-slot.bin"

ZIPDIR="$OUT/zip"
mkdir -p "$ZIPDIR"
cp "$OUT/vtOS-$TAG.bin" "$OUT/vtOS-$TAG-slot.bin" "$ZIPDIR/"
[ -f "$OUT/vtOS-$TAG-dualboot.bin" ] && cp "$OUT/vtOS-$TAG-dualboot.bin" "$ZIPDIR/"
for f in bootloader.bin partition-table.bin TAILSCALE_NOTICES.md; do
  [ -f "$OUT/build/$f" ] && cp "$OUT/build/$f" "$ZIPDIR/"
done
[ -f "$ZIPDIR/TAILSCALE_NOTICES.md" ] || cp modules/modtailscale/THIRD_PARTY_NOTICES.md "$ZIPDIR/TAILSCALE_NOTICES.md" 2>/dev/null || true
[ -f "$ZIPDIR/bootloader.bin" ] && [ -f "$ZIPDIR/partition-table.bin" ] \
  || warn "bootloader.bin / partition-table.bin were not in $FROM_DIR, so the zip will not include them"

ASSETS=("$OUT/vtOS-$TAG.bin")
[ -f "$OUT/vtOS-$TAG-dualboot.bin" ] && ASSETS+=("$OUT/vtOS-$TAG-dualboot.bin")
ASSETS+=("$OUT/vtOS-$TAG-slot.bin")

(cd "$ZIPDIR" && zip -q -r "$OUT/vtOS-$TAG.zip" .)
ASSETS+=("$OUT/vtOS-$TAG.zip")

(cd "$OUT" && sha256 "${ASSETS[@]##*/}" > SHA256SUMS.txt)
ASSETS+=("$OUT/SHA256SUMS.txt")

NOTES="$OUT/NOTES.md"
{
  if [ -n "$NOTES_FILE" ]; then cat "$NOTES_FILE"; printf '\n'; fi
  cat <<EOF
## Which file to flash

| File | Flash at | Use it when |
|---|---|---|
| \`vtOS-$TAG.bin\` | \`0x0\` | a normal single-image install (the README's Option A/B) |
EOF
  if [ -f "$OUT/vtOS-$TAG-dualboot.bin" ]; then
    cat <<EOF
| \`vtOS-$TAG-dualboot.bin\` | \`0x0\` | you want the dual-boot loader menu (vtOS plus the IQ lab). Moving from the single-image layout reformats \`/flash\`, so back it up first; see \`utils/dualboot/README.md\` |
EOF
  fi
  cat <<EOF
| \`vtOS-$TAG-slot.bin\` | \`0x110000\` | the device is already dual-boot and you are only updating vtOS. \`/flash\` and the other slots are untouched |

\`\`\`
esptool.py -p /dev/ttyACM0 -b 460800 --chip esp32s3 write_flash 0x0 vtOS-$TAG.bin
\`\`\`

Built from commit \`$SHORT\` ($BRANCH) on $(date -u '+%Y-%m-%d %H:%M UTC'). Checksums are in \`SHA256SUMS.txt\`.
The zip also holds the bootloader and partition table and the Tailscale third-party notices.
EOF
} > "$NOTES"

# --- publish ----------------------------------------------------------------

echo
info "Release summary"
echo "  repo     $REPO"
echo "  tag      $TAG  (created from $SHORT on $BRANCH)"
for a in "${ASSETS[@]}"; do printf '  asset    %-34s %s\n' "$(basename "$a")" "$(du -h "$a" | cut -f1)"; done
echo "  files    $OUT"

GH_FLAGS=(--repo "$REPO" --target "$SHA" --title "vtOS $TAG" --notes-file "$NOTES" --generate-notes)
[ "$DRAFT" = 1 ] && GH_FLAGS+=(--draft)
[ "$PRERELEASE" = 1 ] && GH_FLAGS+=(--prerelease)

if [ "$DRY_RUN" = 1 ]; then
  echo
  info "Dry run: nothing was tagged or uploaded. The command that would run:"
  printf '  gh release create %s' "$TAG"
  printf ' %q' "${GH_FLAGS[@]}"
  printf ' %q' "${ASSETS[@]}"
  echo
  exit 0
fi

if [ "$ASSUME_YES" != 1 ]; then
  printf '\nPublish %s to %s now? This creates a public tag and release. [y/N] ' "$TAG" "$REPO"
  read -r reply
  case "$reply" in y|Y|yes|YES) ;; *) die "cancelled; images are kept in $OUT" ;; esac
fi

info "Creating the GitHub release"
gh release create "$TAG" "${GH_FLAGS[@]}" "${ASSETS[@]}"
git fetch --quiet --tags origin 2>/dev/null || true
info "Done: https://github.com/$REPO/releases/tag/$TAG"
