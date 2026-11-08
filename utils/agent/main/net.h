/* SPDX-License-Identifier: MIT
 * Wi-Fi station + SNTP. net_init() must run on a task with an internal-RAM
 * stack (PHY calibration / NVS write flash).
 */
#pragma once

#include <stdbool.h>

void net_init(void);
bool net_connect(const char *ssid, const char *password);
bool net_wait_online(int timeout_ms);
/* TLS checks certificate dates: wait for SNTP to set the wall clock. */
bool net_wait_clock(int timeout_ms);
