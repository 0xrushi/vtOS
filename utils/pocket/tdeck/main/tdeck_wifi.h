/* SPDX-License-Identifier: MIT
 * Wi-Fi station + SNTP clock for the HTTP worker.
 *
 * tdeck_wifi_init() must run on a task with an INTERNAL-RAM stack: the radio's
 * PHY calibration and NVS use write flash, which a PSRAM stack cannot do.
 * Credentials come from /sd/pocket/wifi.txt (line 1: SSID, line 2: password,
 * line 2 may be empty or missing for an open network).
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Bring the radio up in station mode (RAM-only config, no flash writes). */
void tdeck_wifi_init(void);
/* Read the credentials file and start connecting; false when none. */
bool tdeck_wifi_connect_from_card(const char *path);
/* The station has an IPv4 address. */
bool tdeck_wifi_online(void);
/* Wait at most `ms` for the wall clock (SNTP); true once it is set. */
bool tdeck_wifi_clock_wait(uint32_t ms);
