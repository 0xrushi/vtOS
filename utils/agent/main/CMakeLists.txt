idf_component_register(
    SRCS "main.c" "console.c" "tools.c" "display_min.c" "net.c" "llm.c"
    INCLUDE_DIRS "."
    REQUIRES driver esp_driver_gpio esp_driver_spi esp_driver_i2c spiffs
             vfs esp_timer esp_hw_support
             esp_wifi esp_netif esp_event nvs_flash esp_http_client esp-tls mbedtls
)
