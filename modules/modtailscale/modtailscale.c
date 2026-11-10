/* Manual Tailscale lifecycle and native SSH socket routing for vtOS. MIT. */
#include "py/runtime.h"
#include "py/mphal.h"
#include "py/mpthread.h"
#include "microlink_internal.h"
#include "tailscale_socket.h"
#include "esp_event.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "nvs.h"
#include "lwip/sockets.h"
#include "lwip/tcpip.h"
#include <string.h>

#define TS_NVS "vtos_tailscale"
#define TS_FDS 8

static StaticSemaphore_t gate_storage;
static SemaphoreHandle_t gate;
static microlink_t *vpn;
static bool stopping;
static bool joining;
static bool wifi_up;
static char auth_key[256];
static char hostname[64];
static int vpn_fds[TS_FDS];
static esp_event_handler_instance_t got_ip_handler, wifi_handler;
static bool got_ip_registered, wifi_registered;

static void lock(void) {
    // First creation is on mp_task, before any worker can call this module.
    if (!gate) {
        gate = xSemaphoreCreateMutexStatic(&gate_storage);
        for (unsigned i = 0; i < TS_FDS; i++) vpn_fds[i] = -1;
    }
    xSemaphoreTake(gate, portMAX_DELAY);
}
static void unlock(void) { xSemaphoreGive(gate); }

bool vtos_tailscale_ip(uint32_t ip) {
    return (ip & 0xffc00000U) == 0x64400000U; // exactly 100.64.0.0/10
}

void vtos_tailscale_release(int fd) {
    if (!gate) return;
    lock();
    for (unsigned i = 0; i < TS_FDS; i++) {
        if (vpn_fds[i] == fd) vpn_fds[i] = -1;
    }
    unlock();
}

int vtos_tailscale_prepare(int fd, uint32_t ip, int64_t deadline,
                           const volatile bool *cancelled) {
    if (!gate) return VTOS_TS_ERR_DOWN;
    bool known = false, tracked = false;
    int64_t last_probe = 0;
    while (esp_timer_get_time() < deadline) {
        if (*cancelled) return -2007; // SSH_ERR_ABORTED
        lock();
        if (!vpn || stopping || !wifi_up ||
            microlink_get_state(vpn) != ML_STATE_CONNECTED) {
            unlock();
            return VTOS_TS_ERR_DOWN;
        }
        if (!tracked) {
            for (unsigned i = 0; i < TS_FDS; i++) {
                if (vpn_fds[i] == -1) {
                    vpn_fds[i] = fd;
                    tracked = true;
                    break;
                }
            }
            if (!tracked) { unlock(); return VTOS_TS_ERR_BIND; }
        }
        LOCK_TCPIP_CORE();
        bool peer_up = ml_wg_mgr_peer_is_up(vpn, ip);
        for (int i = 0; i < vpn->peer_count; i++) {
            if (vpn->peers[i].active && vpn->peers[i].vpn_ip == ip) known = true;
        }
        uint32_t source = microlink_get_vpn_ip(vpn);
        UNLOCK_TCPIP_CORE();
        if (peer_up && source) {
            struct sockaddr_in local = {
                .sin_family = AF_INET,
                .sin_addr.s_addr = htonl(source),
            };
            int result = bind(fd, (struct sockaddr *)&local, sizeof(local));
            unlock();
            return result == 0 ? 0 : VTOS_TS_ERR_BIND;
        }
        int64_t now = esp_timer_get_time();
        if (now - last_probe >= 1000000) {
            // The WG manager owns DISCO state; never mutate it from SSH.
            xQueueSend(vpn->vtos_connect_queue, &ip, 0);
            last_probe = now;
        }
        unlock();
        vTaskDelay(pdMS_TO_TICKS(50));
    }
    return known ? VTOS_TS_ERR_TIMEOUT : VTOS_TS_ERR_PEER;
}

static void network_changed(void *arg, esp_event_base_t base, int32_t id, void *data) {
    lock();
    if (vpn && !stopping) {
        if (base == WIFI_EVENT) {
            wifi_up = false;
        } else {
            wifi_up = true;
            // ANY-bound UDP sockets survive a Wi-Fi address change. Wake the
            // owners of coordination and DERP to rediscover endpoints/reconnect.
            ml_coord_cmd_t cmd = ML_CMD_FORCE_RECONNECT;
            xQueueSend(vpn->coord_cmd_queue, &cmd, 0);
            xEventGroupSetBits(vpn->events, ML_EVT_DERP_RECONNECT);
        }
    }
    unlock();
}

static void unregister_events(void) {
    if (got_ip_registered) {
        esp_event_handler_instance_unregister(IP_EVENT, IP_EVENT_STA_GOT_IP, got_ip_handler);
        got_ip_registered = false;
    }
    if (wifi_registered) {
        esp_event_handler_instance_unregister(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED, wifi_handler);
        wifi_registered = false;
    }
}

static esp_err_t stop_vpn(void) {
    lock();
    if (!vpn) { unlock(); return ESP_OK; }
    if (joining) { unlock(); return ESP_ERR_INVALID_STATE; }
    joining = true;
    stopping = true;
    for (unsigned i = 0; i < TS_FDS; i++) {
        if (vpn_fds[i] >= 0) shutdown(vpn_fds[i], SHUT_RDWR);
    }
    microlink_t *ml = vpn;
    unlock();
    unregister_events();
    esp_err_t err = microlink_stop(ml);
    lock();
    joining = false;
    if (err != ESP_OK) { unlock(); return err; } // retain live context
    microlink_destroy(ml);
    vpn = NULL;
    stopping = false;
    memset(auth_key, 0, sizeof(auth_key));
    unlock();
    return ESP_OK;
}

// gc_sweep_all() finalizes this rooted owner on every MicroPython soft reset.
// Tasks only refer to native storage, never to this object or Python strings.
static mp_obj_t owner_del(mp_obj_t self) {
    MP_THREAD_GIL_EXIT();
    stop_vpn();
    MP_THREAD_GIL_ENTER();
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_1(owner_del_obj, owner_del);
static const mp_rom_map_elem_t owner_locals_table[] = {
    {MP_ROM_QSTR(MP_QSTR___del__), MP_ROM_PTR(&owner_del_obj)},
};
static MP_DEFINE_CONST_DICT(owner_locals, owner_locals_table);
static MP_DEFINE_CONST_OBJ_TYPE(owner_type, MP_QSTR_TailscaleOwner,
    MP_TYPE_FLAG_NONE, locals_dict, &owner_locals);
MP_REGISTER_ROOT_POINTER(mp_obj_t tailscale_owner);

static bool configured(void) {
    nvs_handle_t nvs;
    if (nvs_open(TS_NVS, NVS_READONLY, &nvs) != ESP_OK) return false;
    size_t len = 0;
    bool found = nvs_get_str(nvs, "auth_key", NULL, &len) == ESP_OK && len > 1;
    nvs_close(nvs);
    return found;
}

static void check_error(esp_err_t err) {
    if (err != ESP_OK) mp_raise_msg_varg(&mp_type_OSError,
        MP_ERROR_TEXT("Tailscale: %s"), esp_err_to_name(err));
}

static mp_obj_t ts_configure(mp_obj_t key_obj, mp_obj_t name_obj) {
    const char *key = mp_obj_str_get_str(key_obj);
    const char *name = mp_obj_str_get_str(name_obj);
    size_t key_len = strlen(key), name_len = strlen(name);
    if (key_len <= 11 || key_len >= sizeof(auth_key) || strncmp(key, "tskey-auth-", 11) != 0) {
        mp_raise_ValueError(MP_ERROR_TEXT("expected a tskey-auth- authentication key"));
    }
    if (!name_len || name_len >= sizeof(hostname) || name[0] == '-' || name[name_len - 1] == '-') {
        mp_raise_ValueError(MP_ERROR_TEXT("hostname must be 1-63 letters, digits or hyphens"));
    }
    for (size_t i = 0; i < name_len; i++) {
        char c = name[i];
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '-')) {
            mp_raise_ValueError(MP_ERROR_TEXT("invalid hostname"));
        }
    }
    lock();
    bool running = vpn != NULL;
    unlock();
    if (running) mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("run tailscale down before reconfiguring"));
    nvs_handle_t nvs;
    check_error(nvs_open(TS_NVS, NVS_READWRITE, &nvs));
    esp_err_t err = nvs_set_str(nvs, "auth_key", key);
    if (err == ESP_OK) err = nvs_set_str(nvs, "hostname", name);
    if (err == ESP_OK) err = nvs_commit(nvs);
    nvs_close(nvs);
    check_error(err);
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_2(ts_configure_obj, ts_configure);

static mp_obj_t ts_up(void) {
    lock();
    if (vpn) {
        bool was_stopping = stopping;
        unlock();
        if (was_stopping) mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("VPN still stopping; run tailscale down again"));
        return mp_const_none;
    }
    unlock();
    wifi_ap_record_t ap;
    esp_netif_t *sta = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    esp_netif_ip_info_t ip;
    if (esp_wifi_sta_get_ap_info(&ap) != ESP_OK || !sta ||
        esp_netif_get_ip_info(sta, &ip) != ESP_OK || !ip.ip.addr) {
        mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("connect Wi-Fi with nm before tailscale up"));
    }
    nvs_handle_t nvs;
    check_error(nvs_open(TS_NVS, NVS_READONLY, &nvs));
    size_t size = sizeof(auth_key);
    esp_err_t err = nvs_get_str(nvs, "auth_key", auth_key, &size);
    size = sizeof(hostname);
    if (err == ESP_OK) err = nvs_get_str(nvs, "hostname", hostname, &size);
    nvs_close(nvs);
    check_error(err);
    if (MP_STATE_PORT(tailscale_owner) == MP_OBJ_NULL) {
        mp_obj_base_t *owner = mp_obj_malloc_with_finaliser(mp_obj_base_t, &owner_type);
        owner->type = &owner_type;
        MP_STATE_PORT(tailscale_owner) = MP_OBJ_FROM_PTR(owner);
    }
    // Suppress upstream packet/heartbeat logs on the shared terminal.
    const char *tags[] = {"microlink", "ml_coord", "ml_derp", "ml_wg_mgr", "ml_net_io", "ml_stun", "ml_peer_nvs"};
    for (unsigned i = 0; i < sizeof(tags) / sizeof(tags[0]); i++) esp_log_level_set(tags[i], ESP_LOG_ERROR);
    microlink_config_t config = {
        .auth_key = auth_key, .device_name = hostname,
        .enable_derp = true, .enable_disco = true, .enable_stun = true,
        .max_peers = CONFIG_ML_MAX_PEERS,
    };
    lock();
    wifi_up = true;
    stopping = false;
    vpn = microlink_init(&config);
    if (!vpn) { unlock(); mp_raise_msg(&mp_type_OSError, MP_ERROR_TEXT("VPN allocation failed; check free SRAM/PSRAM")); }
    err = esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                         network_changed, NULL, &got_ip_handler);
    got_ip_registered = err == ESP_OK;
    if (err == ESP_OK) {
        err = esp_event_handler_instance_register(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED,
                             network_changed, NULL, &wifi_handler);
        wifi_registered = err == ESP_OK;
    }
    if (err == ESP_OK) err = microlink_start(vpn);
    unlock();
    if (err != ESP_OK) { stop_vpn(); check_error(err); }
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(ts_up_obj, ts_up);

static mp_obj_t ts_down(void) {
    esp_err_t err;
    MP_THREAD_GIL_EXIT();
    err = stop_vpn();
    MP_THREAD_GIL_ENTER();
    if (err != ESP_OK) mp_raise_msg(&mp_type_OSError,
        MP_ERROR_TEXT("VPN workers still stopping; try tailscale down again"));
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(ts_down_obj, ts_down);

static mp_obj_t ip_object(uint32_t ip) {
    if (!ip) return mp_const_none;
    char text[16];
    microlink_ip_to_str(ip, text);
    return mp_obj_new_str(text, strlen(text));
}

static mp_obj_t ts_ip(void) {
    lock();
    uint32_t ip = vpn && !stopping && wifi_up ? microlink_get_vpn_ip(vpn) : 0;
    unlock();
    return ip_object(ip);
}
static MP_DEFINE_CONST_FUN_OBJ_0(ts_ip_obj, ts_ip);

static mp_obj_t ts_status(void) {
    const char *states[] = {"idle", "wifi_wait", "connecting", "registering", "connected", "reconnecting", "error"};
    lock();
    const char *state = !vpn ? "down" : stopping ? "stopping" : !wifi_up ? "wifi_wait" : states[microlink_get_state(vpn)];
    bool ready = vpn && !stopping && wifi_up && microlink_is_connected(vpn) &&
        (xEventGroupGetBits(vpn->events) & ML_EVT_WG_READY);
    uint32_t ip = ready ? microlink_get_vpn_ip(vpn) : 0;
    unlock();
    mp_obj_t result = mp_obj_new_dict(0);
    mp_obj_dict_store(result, MP_OBJ_NEW_QSTR(MP_QSTR_state), mp_obj_new_str(state, strlen(state)));
    mp_obj_dict_store(result, MP_OBJ_NEW_QSTR(MP_QSTR_connected), mp_obj_new_bool(ready));
    mp_obj_dict_store(result, MP_OBJ_NEW_QSTR(MP_QSTR_configured), mp_obj_new_bool(configured()));
    mp_obj_dict_store(result, MP_OBJ_NEW_QSTR(MP_QSTR_ip), ip_object(ip));
    mp_obj_dict_store(result, MP_OBJ_NEW_QSTR(MP_QSTR_internal_free), mp_obj_new_int_from_uint(heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)));
    mp_obj_dict_store(result, MP_OBJ_NEW_QSTR(MP_QSTR_internal_largest), mp_obj_new_int_from_uint(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)));
    mp_obj_dict_store(result, MP_OBJ_NEW_QSTR(MP_QSTR_psram_free), mp_obj_new_int_from_uint(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)));
    return result;
}
static MP_DEFINE_CONST_FUN_OBJ_0(ts_status_obj, ts_status);

static mp_obj_t ts_peers(void) {
    microlink_peer_info_t peers[CONFIG_ML_MAX_PEERS];
    int count = 0;
    lock();
    if (vpn && !stopping && (xEventGroupGetBits(vpn->events) & ML_EVT_WG_READY)) {
        LOCK_TCPIP_CORE();
        for (int i = 0; i < microlink_get_peer_count(vpn) && count < CONFIG_ML_MAX_PEERS; i++) {
            microlink_peer_info_t info = {0};
            if (microlink_get_peer_info(vpn, i, &info) == ESP_OK && info.online) peers[count++] = info;
        }
        UNLOCK_TCPIP_CORE();
    }
    unlock();
    mp_obj_t result = mp_obj_new_list(0, NULL);
    for (int i = 0; i < count; i++) {
        mp_obj_t row[] = {ip_object(peers[i].vpn_ip),
            mp_obj_new_str(peers[i].hostname, strnlen(peers[i].hostname, sizeof(peers[i].hostname))),
            mp_obj_new_bool(peers[i].direct_path)};
        mp_obj_list_append(result, mp_obj_new_tuple(3, row));
    }
    return result;
}
static MP_DEFINE_CONST_FUN_OBJ_0(ts_peers_obj, ts_peers);

// wait_peer("100.x.y.z", timeout_ms) -> str: this device's own tailnet address.
// Waits until the WireGuard path to that peer is up, nudging discovery exactly
// as the native SSH path does (vtos_tailscale_prepare), but without touching a
// socket. The caller then bind()s an ordinary Python socket to the returned
// address and connect()s: lwIP routes a socket bound to the tunnel address
// through the tunnel. Errors raise OSError with the same VTOS_TS_ERR_* codes
// SSH reports. The wait sleeps in mp_hal_delay_ms() with no lock held, so Ctrl-C
// and a long trackball press still interrupt it.
static mp_obj_t ts_wait_peer(mp_obj_t host_in, mp_obj_t timeout_in) {
    struct in_addr addr;
    if (inet_pton(AF_INET, mp_obj_str_get_str(host_in), &addr) != 1) {
        mp_raise_ValueError(MP_ERROR_TEXT("expected a numeric IPv4 address"));
    }
    uint32_t ip = ntohl(addr.s_addr);
    if (!vtos_tailscale_ip(ip)) {
        mp_raise_ValueError(MP_ERROR_TEXT("not a Tailscale address (100.64.0.0/10)"));
    }
    int64_t deadline = esp_timer_get_time() + (int64_t)mp_obj_get_int(timeout_in) * 1000;
    bool known = false;
    int64_t last_probe = 0;
    while (esp_timer_get_time() < deadline) {
        lock();
        if (!vpn || stopping || !wifi_up ||
            microlink_get_state(vpn) != ML_STATE_CONNECTED) {
            unlock();
            mp_raise_OSError(VTOS_TS_ERR_DOWN);
        }
        LOCK_TCPIP_CORE();
        bool peer_up = ml_wg_mgr_peer_is_up(vpn, ip);
        for (int i = 0; i < vpn->peer_count; i++) {
            if (vpn->peers[i].active && vpn->peers[i].vpn_ip == ip) known = true;
        }
        uint32_t source = microlink_get_vpn_ip(vpn);
        UNLOCK_TCPIP_CORE();
        if (peer_up && source) {
            unlock();
            return ip_object(source);
        }
        int64_t now = esp_timer_get_time();
        if (now - last_probe >= 1000000) {
            xQueueSend(vpn->vtos_connect_queue, &ip, 0);
            last_probe = now;
        }
        unlock();
        mp_hal_delay_ms(50);
    }
    mp_raise_OSError(known ? VTOS_TS_ERR_TIMEOUT : VTOS_TS_ERR_PEER);
}
static MP_DEFINE_CONST_FUN_OBJ_2(ts_wait_peer_obj, ts_wait_peer);

static const mp_rom_map_elem_t globals_table[] = {
    {MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_modtailscale)},
    {MP_ROM_QSTR(MP_QSTR_configure), MP_ROM_PTR(&ts_configure_obj)},
    {MP_ROM_QSTR(MP_QSTR_up), MP_ROM_PTR(&ts_up_obj)},
    {MP_ROM_QSTR(MP_QSTR_down), MP_ROM_PTR(&ts_down_obj)},
    {MP_ROM_QSTR(MP_QSTR_status), MP_ROM_PTR(&ts_status_obj)},
    {MP_ROM_QSTR(MP_QSTR_ip), MP_ROM_PTR(&ts_ip_obj)},
    {MP_ROM_QSTR(MP_QSTR_peers), MP_ROM_PTR(&ts_peers_obj)},
    {MP_ROM_QSTR(MP_QSTR_wait_peer), MP_ROM_PTR(&ts_wait_peer_obj)},
};
static MP_DEFINE_CONST_DICT(globals, globals_table);
const mp_obj_module_t modtailscale_module = {
    .base = {&mp_type_module}, .globals = (mp_obj_dict_t *)&globals,
};
MP_REGISTER_MODULE(MP_QSTR_modtailscale, modtailscale_module);
