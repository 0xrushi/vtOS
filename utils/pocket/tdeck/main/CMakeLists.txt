# Portable app-store state machines shared with every PocketJS native host.
set(shared "$ENV{POCKETJS_ROOT}/engine/host-services")
idf_component_register(
    SRCS "main.c" "tdeck_system.c" "tdeck_wifi.c"
         "${shared}/launcher.c" "${shared}/fs.c" "${shared}/jscan.c" "${shared}/sha256.c"
    INCLUDE_DIRS "." "${shared}"
    REQUIRES pocket_runtime pocket_guest_quickjs pocket_runner pocket_display_esp_lcd
             esp_lcd esp_driver_spi esp_driver_i2c esp_driver_gpio esp_timer
             esp_driver_sdspi sdmmc fatfs vfs esp_hw_support espressif__quickjs-ng
             pocket_worker_esp pocket_http_esp esp_wifi esp_netif esp_event nvs_flash
)

# Launcher package. Default: PocketJS's 320x240 smoke app (blue card).
# Replace with the Pocket Shell package built against pocket.host.json.
set(POCKET_TDECK_PACKAGE "$ENV{POCKETJS_ROOT}/tests/esp-idf/examples/prebuilt/idf-smoke.pocket"
    CACHE FILEPATH "Package to embed")
pocketjs_embed_package(
    TARGET ${COMPONENT_LIB}
    NAME app
    PACKAGE "${POCKET_TDECK_PACKAGE}"
    HOST_CONFIG "../pocket.host.config.ts"
)
