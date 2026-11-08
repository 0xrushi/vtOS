set -x
export CARGO_HOME=/rust/cargo RUSTUP_HOME=/rust/rustup
export PATH=$CARGO_HOME/bin:$PATH
if ! command -v rustup >/dev/null; then curl -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain 1.98.1 --profile minimal; fi
if ! command -v espup >/dev/null; then
  curl -fsSL -o $CARGO_HOME/bin/espup https://github.com/esp-rs/espup/releases/download/v0.17.1/espup-aarch64-unknown-linux-gnu && chmod +x $CARGO_HOME/bin/espup || cargo install espup --version 0.17.1 --locked
fi
espup --version
espup install --name esp --toolchain-version 1.97.0.0 --stable-version 1.98.1 --targets esp32s3 --export-file /rust/export-esp.sh
. /rust/export-esp.sh
cargo +esp -Vv
cd /src
CARGO_PROFILE_RELEASE_PANIC=abort CARGO_PROFILE_RELEASE_LTO=false CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 CARGO_PROFILE_RELEASE_OPT_LEVEL=3 \
cargo +esp build -Zbuild-std=core,alloc --release --locked --no-default-features --target xtensa-esp32s3-none-elf --manifest-path programs/esp-idf/archive/Cargo.toml
ls -la target/xtensa-esp32s3-none-elf/release/*.a 2>/dev/null || find / -name "libpocket*.a" -newer /rust/export-esp.sh 2>/dev/null | head
