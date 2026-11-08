cmake_minimum_required(VERSION 3.22)

# PocketJS checkout; the Docker build mounts it at /src (see build.sh).
if(NOT DEFINED ENV{POCKETJS_ROOT} OR NOT DEFINED ENV{POCKETM5_ROOT})
    message(FATAL_ERROR "Set POCKETJS_ROOT (pocket-nexus/pocketjs) and POCKETM5_ROOT (pocket-nexus/pocket-m5) checkouts")
endif()
set(pocketjs_components "$ENV{POCKETJS_ROOT}/platforms/esp-idf")
list(APPEND EXTRA_COMPONENT_DIRS
    "${pocketjs_components}/pocket_runtime"
    "${pocketjs_components}/pocket_guest_quickjs"
    "${pocketjs_components}/pocket_runner"
    "${pocketjs_components}/pocket_display_esp_lcd"
    "$ENV{POCKETM5_ROOT}/platforms/esp-idf/pocket_worker_esp"
    "$ENV{POCKETM5_ROOT}/platforms/esp-idf/pocket_http_esp")
set(POCKETJS_ROOT "$ENV{POCKETJS_ROOT}" CACHE PATH "PocketJS checkout (worker sources)")
if(NOT DEFINED POCKETJS_RUST_FROM_SOURCE)
    set(POCKETJS_RUST_FROM_SOURCE ON CACHE BOOL "")
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
idf_build_set_property(MINIMAL_BUILD ON)
project(pocket_tdeck)
