set -x
export CARGO_HOME=/rust/cargo RUSTUP_HOME=/rust/rustup
export PATH=/rust/cargo/bin:/root/.bun/bin:$PATH
if ! command -v bun >/dev/null; then curl -fsSL https://bun.sh/install | bash -s "bun-v1.4.2"; fi
bun --version
cd /src && bun install --frozen-lockfile 2>&1 | tail -3
printf '#!/bin/sh\nexec /root/.bun/bin/bun /src/tools/pocket.ts "$@"\n' > /usr/local/bin/pocket; chmod +x /usr/local/bin/pocket
cd /src/tests/esp-idf/examples/prebuilt
rm -rf build sdkconfig
export POCKETJS_CARGO=/rust/rustup/toolchains/esp/bin/cargo
idf.py set-target esp32s3 2>&1 | tail -3
idf.py -DPOCKETJS_RUST_FROM_SOURCE=ON -DPOCKETJS_CARGO=$POCKETJS_CARGO build 2>&1 | tail -40
ls -la build/*.bin
# NOTE: the first idf.py build fails on tests/esp-idf/examples/smoke/main/main.c
# (-Werror=missing-field-initializers: pocket_memory_package gained a `flags`
# field). Patch the initializer to `{..., pocketjs_package_smoke.size, 0}` and
# rerun idf.py build; that produced the 1,552,240 B image.
