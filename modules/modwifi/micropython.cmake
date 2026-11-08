# Add our new C module (passive WiFi promiscuous sniffer)
add_library(usermod_modwifi INTERFACE)

target_sources(usermod_modwifi INTERFACE
  ${CMAKE_CURRENT_LIST_DIR}/modwifi.c
)
target_include_directories(usermod_modwifi INTERFACE
  ${CMAKE_CURRENT_LIST_DIR}
)

# Register the module with MicroPython
target_link_libraries(usermod INTERFACE usermod_modwifi)
