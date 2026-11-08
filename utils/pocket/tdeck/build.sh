#!/bin/sh
# Build the T-Deck PocketJS image in Docker (ESP-IDF 6.0.1, Xtensa Rust).
#   POCKETJS_SRC  pocket-nexus/pocketjs checkout (default: ./pocketjs)
#   PACKAGE       .pocket to embed (default: PocketJS smoke app), e.g. ../tdeck-shell/out/pocket-shell.pocket
#   RUST_HOME     dir holding the espup toolchain (see ../xtensa-runtime-spike.sh)
# Output: build/pocket_tdeck.bin (link address 0x610000, flash it there)
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
: "${POCKETJS_SRC:?set POCKETJS_SRC}"
: "${POCKETM5_SRC:?set POCKETM5_SRC (pocket-nexus/pocket-m5 checkout)}"
: "${RUST_HOME:?set RUST_HOME}"
PKG_ARGS=""
if [ -n "$PACKAGE" ]; then PKG_ARGS="-v $PACKAGE:/pkg/app.pocket:ro"; CMAKE_PKG="-DPOCKET_TDECK_PACKAGE=/pkg/app.pocket"; fi
docker run --rm $PKG_ARGS -e CMAKE_PKG="$CMAKE_PKG" -v "$POCKETJS_SRC":/src -v "$POCKETM5_SRC":/m5 -v "$RUST_HOME":/rust -v "$HERE":/proj \
  -e POCKETJS_ROOT=/src -e POCKETM5_ROOT=/m5 espressif/idf:v6.0.1 bash -c '
export CARGO_HOME=/rust/cargo RUSTUP_HOME=/rust/rustup
export PATH=/rust/cargo/bin:/root/.bun/bin:$PATH
if ! command -v bun >/dev/null; then curl -fsSL https://bun.sh/install | bash -s "bun-v1.4.2"; fi
(cd /src && bun install --frozen-lockfile >/dev/null)
printf "#!/bin/sh\nexec /root/.bun/bin/bun /src/tools/pocket.ts \"\$@\"\n" > /usr/local/bin/pocket
chmod +x /usr/local/bin/pocket
# pocket_worker_esp declares idf >=6.2; it builds on 6.0.1 for our use.
sed -i "s/>=6.2,<6.3/>=6.0,<6.3/" /m5/platforms/esp-idf/pocket_worker_esp/idf_component.yml
# esp_timer_stop_blocking is an IDF 6.2 API; on 6.0.1 stop the timer and give an
# in-flight callback time to finish before it is deleted.
sed -i "s/ESP_ERROR_CHECK(esp_timer_stop_blocking(host->timer, portMAX_DELAY));/esp_timer_stop(host->timer); vTaskDelay(pdMS_TO_TICKS(20));/" /m5/platforms/esp-idf/pocket_worker_esp/src/worker.c
cd /proj
[ -f build/CMakeCache.txt ] || idf.py -DPOCKETJS_RUST_FROM_SOURCE=ON -DPOCKETJS_CARGO=/rust/rustup/toolchains/esp/bin/cargo set-target esp32s3
idf.py -DPOCKETJS_RUST_FROM_SOURCE=ON -DPOCKETJS_CARGO=/rust/rustup/toolchains/esp/bin/cargo $CMAKE_PKG build
'
