/* SPDX-License-Identifier: MIT
 * The T-Deck's app store: a FAT microSD card holding the launcher and the
 * installed applets, plus the launcher's operations and each guest's data.fs.
 *
 *   /sd/pocket/launcher.pocket           the launcher (Pocket Shell)
 *   /sd/pocket/inbox/NAME.pocket         drop packages here; installed at boot
 *   /sd/pocket/apps/<slot>/package.pocket, active   an installed app
 *   /sd/pocket/apps/<slot>/data/         the app's data.fs tree
 *
 * <slot> is the first 16 hex digits of SHA-256 of the app id. The shared
 * launcher/fs state machines are PocketJS's engine/host-services.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"
#include "pocket/package_facts.h"
#include "quickjs.h"

typedef struct tdeck_app tdeck_app_t;

/* Mount the card at /sd; false when no usable card is present. Call after the
 * SPI bus the panel shares has been initialized. */
bool tdeck_sd_mount(void);

/* Create the store directories and install every package in the inbox. */
void tdeck_store_start(void);

/* The launcher: /sd/pocket/launcher.pocket, else the embedded fallback. */
esp_err_t tdeck_load_launcher(const uint8_t *fallback, size_t fallback_size,
                              tdeck_app_t **out);
esp_err_t tdeck_load_slot(const char *slot, tdeck_app_t **out);
void tdeck_app_free(tdeck_app_t *app);
const uint8_t *tdeck_app_bytes(const tdeck_app_t *app, size_t *size);
const pocket_idf_package_facts *tdeck_app_facts(const tdeck_app_t *app);

/* The app's worker domain: source (section 8) or bytecode (section 9) of the
 * admitted variant; false when it declares none. Bytes live as long as the app. */
bool tdeck_app_worker(const tdeck_app_t *app, const char **source, size_t *size);
bool tdeck_app_worker_bytecode(const tdeck_app_t *app, const uint8_t **envelope, size_t *size);

/* A requested switch: slot to boot, or the launcher. */
bool tdeck_switch_pending(void);
bool tdeck_take_switch(char slot[17], bool *launcher);
void tdeck_summon(void);
void tdeck_report_failure(const char *message);

/* Before the guest boots: record it as running, bind its data.fs tree and
 * install globalThis.fs. After its runtime is destroyed: unbind. */
esp_err_t tdeck_bind(JSContext *realm, const tdeck_app_t *app);
void tdeck_unbind(void);

/* The launcher's operations (appTable, appLaunch). */
const char *tdeck_app_table(void);
bool tdeck_launch(const char *output);
