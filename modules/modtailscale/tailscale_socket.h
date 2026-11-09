// Native-only bridge: no Python objects may cross the SSH task boundary.
#pragma once
#include <stdbool.h>
#include <stdint.h>

#define VTOS_TS_CONNECT_MS 45000
#define VTOS_TS_ERR_DOWN -2010
#define VTOS_TS_ERR_PEER -2011
#define VTOS_TS_ERR_TIMEOUT -2012
#define VTOS_TS_ERR_BIND -2013

// dest_ip is in host byte order. These helpers are safe from FreeRTOS tasks.
bool vtos_tailscale_ip(uint32_t dest_ip);
int vtos_tailscale_prepare(int fd, uint32_t dest_ip, int64_t deadline_us,
                           const volatile bool *cancelled);
void vtos_tailscale_release(int fd);
