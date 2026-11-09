// lwIP/FreeRTOS boundary mocks; the transport implementation is inserted intact.
#include <stdbool.h>
#include <stdint.h>
#include <sys/socket.h>
#include <arpa/inet.h>
#include "tailscale_socket.h"
#define TS_FDS 8
#define ML_STATE_CONNECTED 4
#define LOCK_TCPIP_CORE() ((void)0)
#define UNLOCK_TCPIP_CORE() ((void)0)
#define pdMS_TO_TICKS(n) (n)
struct peer { bool active; uint32_t vpn_ip; };
struct ml { int peer_count; int state; struct peer peers[1]; int vtos_connect_queue; } instance;
static struct ml *vpn;
static bool stopping, wifi_up, up;
static int gate, vpn_fds[TS_FDS], mode;
static int64_t now;
int probes;
uint32_t bound_ip;
static void lock(void) {}
static void unlock(void) {}
static int64_t esp_timer_get_time(void) { return now; }
static void vTaskDelay(int ms) { now += (int64_t)ms * 1000; if (mode == 6) stopping = true; }
static int microlink_get_state(struct ml *ml) { return ml->state; }
static bool ml_wg_mgr_peer_is_up(struct ml *ml, uint32_t ip) { return up; }
static uint32_t microlink_get_vpn_ip(struct ml *ml) { return 0x64400102; }
static void xQueueSend(int queue, uint32_t *ip, int timeout) { probes++; if (mode == 0 || mode == 5) up = true; }
static int fake_bind(int fd, const struct sockaddr *address, socklen_t len) {
    bound_ip = ntohl(((const struct sockaddr_in *)address)->sin_addr.s_addr);
    return mode == 5 ? -1 : 0;
}
#define bind fake_bind
/* TRANSPORT */

int run_case(int selected) {
    mode = selected;
    now = 1000000;
    probes = 0;
    bound_ip = 0;
    gate = 1;
    stopping = false;
    wifi_up = mode != 7;
    up = false;
    instance.peer_count = mode == 2 ? 0 : 1;
    instance.peers[0].active = true;
    instance.peers[0].vpn_ip = 0x64400103;
    instance.state = ML_STATE_CONNECTED;
    vpn = mode == 1 ? 0 : &instance;
    for (int i = 0; i < TS_FDS; i++) vpn_fds[i] = -1;
    volatile bool cancelled = mode == 4;
    int result = vtos_tailscale_prepare(42, 0x64400103, now + 2000000, &cancelled);
    vtos_tailscale_release(42);
    return result;
}
int tracked_count(void) {
    int count = 0;
    for (int i = 0; i < TS_FDS; i++) count += vpn_fds[i] >= 0;
    return count;
}
