# ESP32-S3 RF SRAM IQ playback lab module (GPL-3.0-or-later, from
# whstudio123/esp32-iq-tx -- see mod_iqtx.c header)
add_library(usermod_iqtx INTERFACE)

target_sources(usermod_iqtx INTERFACE
  ${CMAKE_CURRENT_LIST_DIR}/mod_iqtx.c
)
target_include_directories(usermod_iqtx INTERFACE ${CMAKE_CURRENT_LIST_DIR})

# needs the PHY cert-test path (esp_phy_rftest_* / esp_phy_wifi_tx_tone
# in librftest.a) -- CONFIG_ESP_PHY_ENABLE_CERT_TEST=y is set in the
# board sdkconfig; esp_wifi comes via the port's main component link
target_link_libraries(usermod_iqtx INTERFACE idf::esp_phy idf::esp_wifi)

# Register the module with MicroPython
target_link_libraries(usermod INTERFACE usermod_iqtx)
