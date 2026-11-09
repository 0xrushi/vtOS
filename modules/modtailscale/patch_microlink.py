#!/usr/bin/env python3
"""Apply the vtOS integration to the pinned MicroLink components, idempotently.

Only accepts the exact upstream fragments (or their already-patched forms).
Keep this patch separate from Component Manager's downloaded source tree.
"""
from pathlib import Path
import re
import sys


def replace(text, old, new):
    if new and new in text:
        return text
    if text.count(old) != 1:
        raise ValueError("MicroLink source mismatch: " + repr(old[:100]))
    return text.replace(old, new, 1)


def edit(root, path, transform):
    file = root / path
    source = file.read_text()
    result = transform(source)
    if source != result:
        file.write_text(result)


def patch_internal(s):
    return replace(s, "    /* Task handles */", """    /* vtOS: worker acknowledgements and on-demand peer requests. */
    EventBits_t vtos_tasks_started;
    QueueHandle_t vtos_connect_queue;

    /* Task handles */""")


def patch_api(s):
    s = replace(s, '#include "microlink_internal.h"',
                '#include "microlink_internal.h"\n#include "ml_vtos.h"\n#include "freertos/idf_additions.h"')
    s = replace(s, "    /* Create queues */", """    /* Create queues */
    ml->vtos_connect_queue = xQueueCreate(8, sizeof(uint32_t));""")
    s = replace(s, "    if (!ml->derp_tx_queue || !ml->disco_rx_queue || !ml->wg_rx_queue ||",
                "    if (!ml->vtos_connect_queue || !ml->derp_tx_queue || !ml->disco_rx_queue || !ml->wg_rx_queue ||")
    s = replace(s, '        free(ml);\n        return NULL;\n    }\n\n    /* Create queues */',
                '        ml_peer_nvs_deinit();\n        free(ml);\n        return NULL;\n    }\n\n    /* Create queues */')
    for task, bit, caps in [
        ("net_io", "NET", "MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT"),
        ("derp_tx", "DERP", "MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT"),
        ("coord", "COORD", "MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT"),
        # Peer cache writes use NVS, which requires an internal task stack.
        ("wg_mgr", "WG", "MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT"),
    ]:
        macro = task.upper()
        old = f'''    ret = xTaskCreatePinnedToCore(ml_{task}_task, "ml_{task}", ML_TASK_{macro}_STACK,
                                   ml, ML_TASK_{macro}_PRIO, &ml->{task}_task, ML_TASK_{macro}_CORE);'''
        new = f'''    ret = xTaskCreatePinnedToCoreWithCaps(ml_{task}_task, "ml_{task}", ML_TASK_{macro}_STACK,
                                   ml, ML_TASK_{macro}_PRIO, &ml->{task}_task, ML_TASK_{macro}_CORE,
                                   {caps});
    if (ret == pdPASS) ml->vtos_tasks_started |= ML_VTOS_{bit}_EXIT;'''
        s = replace(s, old, new)
    start = "esp_err_t microlink_stop(microlink_t *ml) {"
    end = "\nvoid microlink_destroy(microlink_t *ml) {"
    new = start + "\n    return ml_vtos_stop(ml);\n}\n"
    if new not in s:
        a, b = s.index(start), s.index(end)
        s = s[:a] + new + s[b:]
    s = replace(s, "    microlink_stop(ml);\n\n    /* Deinitialize peer NVS */",
                """    /* Never free a context while one of its workers is still running. */
    if (microlink_stop(ml) != ESP_OK) return;

    /* Deinitialize peer NVS */""")
    s = replace(s, "    /* Delete queues */", """    /* Delete queues */
    if (ml->vtos_connect_queue) vQueueDelete(ml->vtos_connect_queue);""")
    return s


def patch_task(s, bit):
    s = replace(s, '#include "microlink_internal.h"',
                '#include "microlink_internal.h"\n#include "ml_vtos.h"')
    # These are all exits of the one worker in each translation unit.
    s = s.replace("vTaskDelete(NULL);", f"ml_vtos_task_exit(ml, ML_VTOS_{bit}_EXIT);")
    return s


def patch_coord(s):
    s = patch_task(s, "COORD")
    return replace(s, "    memset(&noise, 0, sizeof(noise));\n\n    ESP_LOGI(TAG, \"Coord task exiting\");",
                   """    memset(&noise, 0, sizeof(noise));
    free(s_server_extra_data);
    s_server_extra_data = NULL;
    s_server_extra_data_len = 0;

    ESP_LOGI(TAG, "Coord task exiting");""")


def patch_derp(s):
    s = patch_task(s, "DERP")
    s = replace(s, '    ESP_LOGI(TAG, "DERP I/O task exiting");',
                '    ml_derp_disconnect(ml);\n    ESP_LOGI(TAG, "DERP I/O task exiting");')
    s = s.replace("attempt < 3 && !ml->derp.connected;", "attempt < 3 && !ml->derp.connected &&\n                     !(xEventGroupGetBits(ml->events) & ML_EVT_SHUTDOWN_REQUEST);")
    # Failed TLS handshakes can already have closed the fd. Free their TLS
    # allocations as well, before another attempt reinitializes the contexts.
    s = replace(s, "    /* TLS setup */", "    /* TLS setup */\n    ml_derp_disconnect(ml);")
    s = replace(s, "        mbedtls_ssl_free(&ml->derp.ssl);\n        mbedtls_ssl_config_free(&ml->derp.ssl_conf);\n        mbedtls_ctr_drbg_free(&ml->derp.ctr_drbg);\n        mbedtls_entropy_free(&ml->derp.entropy);\n", "") if "        mbedtls_ssl_free(&ml->derp.ssl);" in s else s
    s = replace(s, "    /* Drain TX queue */", """    mbedtls_ssl_free(&ml->derp.ssl);
    mbedtls_ssl_config_free(&ml->derp.ssl_conf);
    mbedtls_ctr_drbg_free(&ml->derp.ctr_drbg);
    mbedtls_entropy_free(&ml->derp.entropy);

    /* Drain TX queue */""")
    return s


def patch_wg(s):
    s = patch_task(s, "WG")
    s = replace(s, "            s_wg_output_pcb->local_port = 51820;",
                "            s_wg_output_pcb->local_port = ml->disco_local_port;")
    # Keep a busy video stream from monopolizing the lwIP core lock.
    for queue, packet in (("disco_rx_queue", "disco_pkt"), ("wg_rx_queue", "wg_pkt")):
        s = replace(s, f"        while (xQueueReceive(ml->{queue}, &{packet}, 0) == pdTRUE) {{",
                    f"        for (int n = 0; n < 8 && xQueueReceive(ml->{queue}, &{packet}, 0) == pdTRUE; n++) {{")
    # All manager mutations and WG crypto serialize with TCP timers/output.
    # Blocking socket/NVS/esp_netif calls below explicitly release the lock.
    s = replace(s, "    /* Initialize WireGuard interface (magicsock mode) */",
                "    LOCK_TCPIP_CORE();\n    /* Initialize WireGuard interface (magicsock mode) */")
    s = replace(s, '    ESP_LOGI(TAG, "Accepting peer updates");',
                '    UNLOCK_TCPIP_CORE();\n    ESP_LOGI(TAG, "Accepting peer updates");')
    s = replace(s, "        /* Process peer updates from coord task */", """        LOCK_TCPIP_CORE();
        /* Process peer updates from coord task */""")
    s = replace(s, "        process_peer_updates(ml);", """        process_peer_updates(ml);
        wg_update_vpn_ip(ml);
        uint32_t requested_ip;
        while (xQueueReceive(ml->vtos_connect_queue, &requested_ip, 0) == pdTRUE) {
            int peer = find_peer_by_ip(ml, requested_ip);
            if (peer >= 0) {
                ml_wg_mgr_trigger_handshake(ml, requested_ip);
                disco_send_call_me_maybe(ml, peer);
                disco_send_ping_to_peer(ml, peer, true);
            }
        }""")
    s = replace(s, "        vTaskDelay(pdMS_TO_TICKS(10));", "        UNLOCK_TCPIP_CORE();\n        vTaskDelay(pdMS_TO_TICKS(10));")
    s = replace(s, "    /* Shutdown WireGuard interface */", "    LOCK_TCPIP_CORE();\n    /* Shutdown WireGuard interface */")
    s = replace(s, "        vTaskDelay(pdMS_TO_TICKS(100));\n        netif_remove(netif);", "        netif_remove(netif);")
    s = replace(s, '    ESP_LOGI(TAG, "WG Manager task exiting");', """    if (s_wg_output_pcb) {
        udp_remove(s_wg_output_pcb);
        s_wg_output_pcb = NULL;
    }
    UNLOCK_TCPIP_CORE();
    ESP_LOGI(TAG, "WG Manager task exiting");""")
    s = replace(s, "    return ml_sendto(ml->disco_sock4, data, len, MSG_DONTWAIT,\n                  (struct sockaddr *)&dest, sizeof(dest));", """    UNLOCK_TCPIP_CORE();
    int sent = ml_sendto(ml->disco_sock4, data, len, MSG_DONTWAIT,
                        (struct sockaddr *)&dest, sizeof(dest));
    LOCK_TCPIP_CORE();
    return sent;""")
    s = replace(s, "    ml_peer_nvs_save(p);", "    UNLOCK_TCPIP_CORE();\n    ml_peer_nvs_save(p);\n    LOCK_TCPIP_CORE();")
    # The WG manager is the sole writer of the peer table; the esp_netif
    # query must run unlocked because its IPC waits for the TCPIP thread.
    s = replace(s, '    esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");',
                '    UNLOCK_TCPIP_CORE();\n    esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");')
    s = replace(s, "    /* 2. STUN public endpoint (for cross-NAT peers) */", "    LOCK_TCPIP_CORE();\n    /* 2. STUN public endpoint (for cross-NAT peers) */")
    # Register through lwIP, which assigns a unique interface number and
    # initializes all netif state. Input is synchronous under the core lock,
    # so no queued input retains a freed netif during shutdown.
    a = "    /* Initialize WireGuard netif */"
    b = "    /* Bring interface up */"
    new = """    /* vtOS: register the netif under the lwIP core lock. */
    ip4_addr_t ip, mask, gateway;
    ip.addr = htonl(ml->vpn_ip ? ml->vpn_ip : 0x64400001);
    IP4_ADDR(&mask, 255, 192, 0, 0);
    ip4_addr_set_zero(&gateway);
    if (!netif_add(netif, &ip, &mask, &gateway, &wg_init,
                   wireguardif_init, ip_input)) {
        free(netif);
        return ESP_FAIL;
    }

"""
    if new not in s:
        start, end = s.index(a), s.index(b)
        s = s[:start] + new + s[end:]
    return s


def patch_wireguard(s):
    # Upstream has raw printf calls on the per-packet receive path despite
    # WG_DEBUG_LOGGING=0. Avoid blocking on serial output under the core lock.
    s = replace(s, "#define WG_DEBUG(...) do {} while(0)",
                "#define WG_DEBUG(...) do { if (0) printf(__VA_ARGS__); } while (0)")
    s = re.sub(r"^([ \t]*)printf\(", r"\1WG_DEBUG(", s, flags=re.MULTILINE)
    # Upstream shutdown only cancelled its timer, leaking the whole device
    # and every peer's session keys across down/up cycles.
    return replace(s, "\tsys_untimeout(wireguardif_tmr, device);\n}", """\tsys_untimeout(wireguardif_tmr, device);
    for (u8_t i = 0; i < WIREGUARD_MAX_PEERS; ++i) {
        if (device->peers[i].valid) wireguardif_remove_peer(netif, i);
    }
    if (device->udp_pcb) udp_remove(device->udp_pcb);
    memset(device, 0, sizeof(*device));
    mem_free(device);
    netif->state = NULL;
}""")


def main(microlink, wireguard):
    ml, wg = Path(microlink), Path(wireguard)
    edit(ml, "include/microlink_internal.h", patch_internal)
    edit(ml, "src/microlink.c", patch_api)
    edit(ml, "src/ml_net_io.c", lambda s: patch_task(s, "NET"))
    edit(ml, "src/ml_coord.c", patch_coord)
    edit(ml, "src/ml_derp.c", patch_derp)
    edit(ml, "src/ml_wg_mgr.c", patch_wg)
    edit(wg, "src/wireguardif.c", patch_wireguard)


if __name__ == "__main__":
    main(*sys.argv[1:])
