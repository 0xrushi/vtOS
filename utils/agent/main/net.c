/* SPDX-License-Identifier: MIT */
#include "net.h"

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include <string.h>
#include <time.h>

#define TAG "net"

static volatile bool online_;
static volatile bool want_;
static bool sntp_;

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)data;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        online_ = false;
        if (want_) {
            esp_wifi_connect();
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        online_ = true;
        if (!sntp_) {
            sntp_ = true;
            esp_sntp_config_t cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
            esp_netif_sntp_init(&cfg);
        }
    }
}

void net_init(void)
{
    esp_err_t err = nvs_flash_init();
    if (err != ESP_OK) {
        /* The loader keeps its own keys in this partition: never erase it. */
        ESP_LOGE(TAG, "nvs: %s", esp_err_to_name(err));
        return;
    }
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    cfg.static_rx_buf_num = 4;
    cfg.dynamic_rx_buf_num = 8;
    cfg.dynamic_tx_buf_num = 8;
    cfg.cache_tx_buf_num = 0;
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());
}

bool net_connect(const char *ssid, const char *password)
{
    if (!ssid || !ssid[0]) {
        return false;
    }
    wifi_config_t config = {0};
    strlcpy((char *)config.sta.ssid, ssid, sizeof config.sta.ssid);
    strlcpy((char *)config.sta.password, password ? password : "", sizeof config.sta.password);
    config.sta.threshold.authmode = (password && password[0]) ? WIFI_AUTH_WPA_PSK : WIFI_AUTH_OPEN;
    if (esp_wifi_set_config(WIFI_IF_STA, &config) != ESP_OK) {
        return false;
    }
    want_ = true;
    return esp_wifi_connect() == ESP_OK;
}

bool net_wait_online(int timeout_ms)
{
    for (int waited = 0; waited < timeout_ms; waited += 100) {
        if (online_) {
            return true;
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    return online_;
}

bool net_wait_clock(int timeout_ms)
{
    for (int waited = 0; waited < timeout_ms; waited += 100) {
        if (time(NULL) > 1700000000) {
            return true;
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
    return time(NULL) > 1700000000;
}
