#!/bin/sh
# Build the agent payload (ESP-IDF 5.5.1 in Docker, same as the loader).
# Output: build/agent.bin, flashed at 0x610000 (the dual-boot "agent" slot).
set -e
cd "$(dirname "$0")"
docker run --rm -v "$PWD":/work -w /work espressif/idf:v5.5.1 idf.py set-target esp32s3 build
