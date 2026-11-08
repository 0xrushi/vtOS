idf_component_register(SRCS "main.c" "display_min.c"
                       INCLUDE_DIRS "."
                       REQUIRES app_update nvs_flash esp_timer
                       esp_driver_gpio esp_driver_spi esp_driver_usb_serial_jtag)
