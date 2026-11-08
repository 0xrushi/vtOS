/* SPDX-License-Identifier: MIT */
#include "tdeck_wifi.h"

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_wifi.h"
#include "nvs_flash.h"

#include <stdio.h>
#include <string.h>
#include <time.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define TAG "tdeck_wifi"

static volatile bool online;
static volatile bool want_connect;
static bool sntp_started;

static void on_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
  (void)arg;
  (void)data;
  if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
    online = false;
    if (want_connect) {
      ESP_LOGW(TAG, "disconnected, retrying");
      esp_wifi_connect();
    }
  } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
    online = true;
    ip_event_got_ip_t *got = data;
    ESP_LOGI(TAG, "got address " IPSTR, IP2STR(&got->ip_info.ip));
    if (!sntp_started) {
      sntp_started = true;
      esp_sntp_config_t cfg = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
      esp_netif_sntp_init(&cfg);
    }
  }
}

void tdeck_wifi_init(void) {
  esp_err_t err = nvs_flash_init();
  if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    /* The loader keeps its own keys here; never erase the partition. */
    ESP_LOGE(TAG, "NVS needs erasing (%s); Wi-Fi disabled", esp_err_to_name(err));
    return;
  }
  ESP_ERROR_CHECK(esp_netif_init());
  ESP_ERROR_CHECK(esp_event_loop_create_default());
  esp_netif_create_default_wifi_sta();
  wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
  ESP_ERROR_CHECK(esp_wifi_init(&cfg));
  ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
  ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_event, NULL));
  ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_event, NULL));
  ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
  ESP_ERROR_CHECK(esp_wifi_start());
  ESP_LOGI(TAG, "radio started");
}

bool tdeck_wifi_connect_from_card(const char *path) {
  FILE *file = fopen(path, "r");
  if (!file) {
    ESP_LOGW(TAG, "no %s: create it with the SSID on line 1 and the password on line 2", path);
    return false;
  }
  char ssid[33] = {0}, pass[65] = {0};
  if (!fgets(ssid, sizeof ssid, file)) {
    fclose(file);
    return false;
  }
  if (!fgets(pass, sizeof pass, file))
    pass[0] = '\0';
  fclose(file);
  ssid[strcspn(ssid, "\r\n")] = '\0';
  pass[strcspn(pass, "\r\n")] = '\0';
  if (!ssid[0])
    return false;
  wifi_config_t config = {0};
  strlcpy((char *)config.sta.ssid, ssid, sizeof config.sta.ssid);
  strlcpy((char *)config.sta.password, pass, sizeof config.sta.password);
  config.sta.threshold.authmode = pass[0] ? WIFI_AUTH_WPA_PSK : WIFI_AUTH_OPEN;
  if (esp_wifi_set_config(WIFI_IF_STA, &config) != ESP_OK)
    return false;
  want_connect = true;
  ESP_LOGI(TAG, "connecting to \"%s\"", ssid);
  return esp_wifi_connect() == ESP_OK;
}

bool tdeck_wifi_online(void) { return online; }

bool tdeck_wifi_clock_wait(uint32_t ms) {
  const time_t floor = 1700000000; /* anything after 2023 means SNTP ran */
  const int step = 100;
  for (uint32_t waited = 0;; waited += step) {
    if (time(NULL) > floor)
      return true;
    if (waited >= ms || !online)
      return false;
    vTaskDelay(pdMS_TO_TICKS(step));
  }
}
