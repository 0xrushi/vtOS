idf_component_get_property(microlink_dir microlink COMPONENT_DIR)
idf_component_get_property(microlink_lib microlink COMPONENT_LIB)
idf_component_get_property(wireguard_dir wireguard_lwip COMPONENT_DIR)

# Fail closed if the pinned vendor sources no longer match our integration.
execute_process(
    COMMAND python3 ${CMAKE_CURRENT_LIST_DIR}/patch_microlink.py
        ${microlink_dir} ${wireguard_dir}
    RESULT_VARIABLE patch_result
)
if(NOT patch_result EQUAL 0)
    message(FATAL_ERROR "Could not apply vtOS MicroLink integration patches")
endif()
target_sources(${microlink_lib} PRIVATE ${CMAKE_CURRENT_LIST_DIR}/ml_vtos.c)
target_include_directories(${microlink_lib} PRIVATE ${CMAKE_CURRENT_LIST_DIR})
# Per-packet INFO logging overwhelms the on-device terminal during video.
target_compile_definitions(${microlink_lib} PRIVATE LOG_LOCAL_LEVEL=ESP_LOG_WARN)

add_library(usermod_modtailscale INTERFACE)
target_sources(usermod_modtailscale INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}/modtailscale.c)
target_include_directories(usermod_modtailscale INTERFACE
    ${CMAKE_CURRENT_LIST_DIR}
    ${microlink_dir}/include)
# Link through the ESP32 port's IDF requirements. MicroPython's recursive
# usermod scanner cannot parse the generator expressions exported by mbedTLS.
list(APPEND IDF_COMPONENTS microlink)
target_link_libraries(usermod INTERFACE usermod_modtailscale)
