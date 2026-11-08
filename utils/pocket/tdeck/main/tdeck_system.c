/* SPDX-License-Identifier: MIT */
#include "tdeck_system.h"

#include "pocket_profile.h"
#include "driver/sdspi_host.h"
#include "driver/spi_master.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_vfs_fat.h"
#include "fs.h"
#include "host_services.h"
#include "launcher.h"
#include "sdmmc_cmd.h"
#include "sha256.h"

#include <dirent.h>
#include <errno.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define TAG "tdeck_system"

#define SD_MOUNT "/sd"
#define SD_CS 39
#define ROOT SD_MOUNT "/pocket"
#define APPS ROOT "/apps"
#define INBOX ROOT "/inbox"
#define LAUNCHER_FILE ROOT "/launcher.pocket"

struct tdeck_app {
  uint8_t *bytes;
  size_t size;
  bool owned;
  bool launcher;
  pocket_idf_package_facts facts;
};

static bool mounted;
static bool launcher_ready;

/* ---- SD card -------------------------------------------------------------- */

bool tdeck_sd_mount(void) {
  const esp_vfs_fat_sdmmc_mount_config_t mount = {
      .format_if_mount_failed = false,
      .max_files = 8,
      .allocation_unit_size = 16 * 1024,
  };
  sdmmc_host_t host = SDSPI_HOST_DEFAULT();
  host.slot = SPI2_HOST;
  host.max_freq_khz = 20000;
  sdspi_device_config_t slot = SDSPI_DEVICE_CONFIG_DEFAULT();
  slot.gpio_cs = SD_CS;
  slot.host_id = SPI2_HOST;
  sdmmc_card_t *card = NULL;
  esp_err_t err = esp_vfs_fat_sdspi_mount(SD_MOUNT, &host, &slot, &mount, &card);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "SD card not mounted: %s", esp_err_to_name(err));
    return false;
  }
  ESP_LOGI(TAG, "SD card mounted: %.1f GB",
           (double)card->csd.capacity * card->csd.sector_size / 1e9);
  mounted = true;
  return true;
}

/* ---- host hooks for the shared sources ------------------------------------ */

uint64_t services_host_ms(void) { return (uint64_t)(esp_timer_get_time() / 1000); }
uint64_t services_host_us(void) { return (uint64_t)esp_timer_get_time(); }
uint64_t services_host_epoch_ms(void) { return (uint64_t)time(NULL) * 1000U; }
void services_host_random(uint8_t *out, size_t length) { esp_fill_random(out, length); }
void services_host_release(void *held) { heap_caps_free(held); }

static uint8_t *read_file(const char *path, size_t *size) {
  FILE *file = fopen(path, "rb");
  if (!file)
    return NULL;
  fseek(file, 0, SEEK_END);
  long length = ftell(file);
  fseek(file, 0, SEEK_SET);
  if (length <= 0) {
    fclose(file);
    return NULL;
  }
  uint8_t *bytes = heap_caps_malloc((size_t)length, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!bytes) {
    fclose(file);
    return NULL;
  }
  size_t got = fread(bytes, 1, (size_t)length, file);
  fclose(file);
  if (got != (size_t)length) {
    heap_caps_free(bytes);
    return NULL;
  }
  *size = (size_t)length;
  return bytes;
}

static bool admit(const uint8_t *bytes, size_t size, pocket_idf_package_facts *facts) {
  memset(facts, 0, sizeof *facts);
  facts->struct_size = sizeof *facts;
  int32_t code = pocket_idf_package_open(bytes, size, pocket_profile.facts,
                                         pocket_profile.host_abi,
                                         pocket_profile.profile_hash, facts);
  if (code != POCKET_PACKAGE_OPEN_OK) {
    ESP_LOGW(TAG, "package not admitted (code %d)", (int)code);
    return false;
  }
  return true;
}

const char *launcher_host_apps_directory(void) { return APPS; }

bool launcher_host_slot_path(const char *slot, const char *name, char *out, size_t capacity) {
  int n = name ? snprintf(out, capacity, APPS "/%s/%s", slot, name)
               : snprintf(out, capacity, APPS "/%s", slot);
  return n > 0 && (size_t)n < capacity;
}

bool launcher_host_active_hash(const char *slot, uint64_t *hash) {
  char path[96];
  if (!launcher_host_slot_path(slot, "active", path, sizeof path))
    return false;
  FILE *file = fopen(path, "rb");
  if (!file)
    return false;
  unsigned long long value = 0;
  int ok = fscanf(file, "%16llx", &value);
  fclose(file);
  if (ok != 1 || value == 0)
    return false;
  *hash = (uint64_t)value;
  return true;
}

bool launcher_host_load(const char *slot, uint64_t hash, LauncherPackage *out, void **held) {
  char path[96];
  if (!launcher_host_slot_path(slot, "package.pocket", path, sizeof path))
    return false;
  size_t size = 0;
  uint8_t *bytes = read_file(path, &size);
  if (!bytes)
    return false;
  pocket_idf_package_facts facts;
  if (!admit(bytes, size, &facts) || facts.package_hash != hash) {
    heap_caps_free(bytes);
    return false;
  }
  out->plan = (const char *)facts.plan;
  out->plan_length = facts.plan_length;
  out->hash = facts.package_hash;
  out->bytes = size;
  *held = bytes;
  return true;
}

/* link.c is not built; the shared launcher module only needs these. */

/* ---- the store ------------------------------------------------------------ */

static void make_dir(const char *path) {
  if (mkdir(path, 0777) != 0 && errno != EEXIST)
    ESP_LOGW(TAG, "mkdir %s: errno %d", path, errno);
}

static bool write_file(const char *path, const uint8_t *bytes, size_t size) {
  FILE *file = fopen(path, "wb");
  if (!file)
    return false;
  size_t put = fwrite(bytes, 1, size, file);
  bool ok = fclose(file) == 0 && put == size;
  return ok;
}

static void install_package(const char *name, const uint8_t *bytes, size_t size) {
  pocket_idf_package_facts facts;
  if (!admit(bytes, size, &facts)) {
    ESP_LOGW(TAG, "inbox/%s: skipped (not admitted by this host)", name);
    return;
  }
  uint64_t active = 0;
  if (launcher_host_active_hash(facts.slot, &active) && active == facts.package_hash) {
    ESP_LOGI(TAG, "inbox/%s: already installed", name);
    return;
  }
  char dir[64], package[96], temp[96], marker[96];
  launcher_host_slot_path(facts.slot, NULL, dir, sizeof dir);
  launcher_host_slot_path(facts.slot, "package.pocket", package, sizeof package);
  launcher_host_slot_path(facts.slot, "package.tmp", temp, sizeof temp);
  launcher_host_slot_path(facts.slot, "active", marker, sizeof marker);
  make_dir(dir);
  if (!write_file(temp, bytes, size)) {
    ESP_LOGE(TAG, "inbox/%s: write failed", name);
    return;
  }
  remove(package); /* FAT refuses a rename onto an existing name */
  if (rename(temp, package) != 0) {
    ESP_LOGE(TAG, "inbox/%s: rename failed", name);
    return;
  }
  char hex[24];
  int n = snprintf(hex, sizeof hex, "%016" PRIx64 "\n", facts.package_hash);
  if (!write_file(marker, (const uint8_t *)hex, (size_t)n))
    ESP_LOGE(TAG, "inbox/%s: marker write failed", name);
  ESP_LOGI(TAG, "installed %s as slot %s (%u bytes)", name, facts.slot, (unsigned)size);
}

void tdeck_store_start(void) {
  if (!mounted)
    return;
  make_dir(ROOT);
  make_dir(APPS);
  make_dir(INBOX);
  DIR *directory = opendir(INBOX);
  if (!directory)
    return;
  struct dirent *entry;
  while ((entry = readdir(directory)) != NULL) {
    size_t length = strlen(entry->d_name);
    if (length < 8 || strcmp(entry->d_name + length - 7, ".pocket") != 0)
      continue;
    char path[160];
    snprintf(path, sizeof path, INBOX "/%s", entry->d_name);
    size_t size = 0;
    uint8_t *bytes = read_file(path, &size);
    if (!bytes) {
      ESP_LOGW(TAG, "inbox/%s: unreadable", entry->d_name);
      continue;
    }
    install_package(entry->d_name, bytes, size);
    heap_caps_free(bytes);
  }
  closedir(directory);
}

/* ---- apps ----------------------------------------------------------------- */

static tdeck_app_t *make_app(uint8_t *bytes, size_t size, bool owned) {
  tdeck_app_t *app = calloc(1, sizeof *app);
  if (!app)
    return NULL;
  app->bytes = bytes;
  app->size = size;
  app->owned = owned;
  if (!admit(bytes, size, &app->facts)) {
    free(app);
    return NULL;
  }
  return app;
}

static LauncherPackage package_of(const tdeck_app_t *app) {
  return (LauncherPackage){.plan = (const char *)app->facts.plan,
                           .plan_length = app->facts.plan_length,
                           .hash = app->facts.package_hash,
                           .bytes = app->size};
}

esp_err_t tdeck_load_launcher(const uint8_t *fallback, size_t fallback_size, tdeck_app_t **out) {
  tdeck_app_t *app = NULL;
  size_t size = 0;
  uint8_t *bytes = mounted ? read_file(LAUNCHER_FILE, &size) : NULL;
  if (bytes) {
    app = make_app(bytes, size, true);
    if (!app)
      heap_caps_free(bytes);
  } else {
    ESP_LOGW(TAG, "no %s; using the embedded fallback app", LAUNCHER_FILE);
  }
  if (!app)
    app = make_app((uint8_t *)fallback, fallback_size, false);
  if (!app)
    return ESP_FAIL;
  app->launcher = true;
  if (!launcher_ready) {
    LauncherPackage package = package_of(app);
    launcher_init(app->facts.slot, &package);
    launcher_ready = true;
  }
  *out = app;
  return ESP_OK;
}

esp_err_t tdeck_load_slot(const char *slot, tdeck_app_t **out) {
  char path[96];
  uint64_t active = 0;
  if (!launcher_host_slot_path(slot, "package.pocket", path, sizeof path) ||
      !launcher_host_active_hash(slot, &active))
    return ESP_ERR_NOT_FOUND;
  size_t size = 0;
  uint8_t *bytes = read_file(path, &size);
  if (!bytes)
    return ESP_ERR_NOT_FOUND;
  tdeck_app_t *app = make_app(bytes, size, true);
  if (!app) {
    heap_caps_free(bytes);
    return ESP_FAIL;
  }
  if (app->facts.package_hash != active) {
    tdeck_app_free(app);
    return ESP_ERR_INVALID_CRC;
  }
  *out = app;
  return ESP_OK;
}

void tdeck_app_free(tdeck_app_t *app) {
  if (!app)
    return;
  if (app->owned)
    heap_caps_free(app->bytes);
  free(app);
}

const uint8_t *tdeck_app_bytes(const tdeck_app_t *app, size_t *size) {
  *size = app->size;
  return app->bytes;
}

const pocket_idf_package_facts *tdeck_app_facts(const tdeck_app_t *app) {
  return &app->facts;
}

static uint32_t le32(const uint8_t *p) {
  return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

static uint64_t le64(const uint8_t *p) {
  return (uint64_t)le32(p) | (uint64_t)le32(p + 4) << 32;
}

/* A section of the admitted variant (the one whose hash, variant offset 24, is
 * facts->variant_hash): kind 8 = NUL-terminated worker source, 9 = its bytecode
 * envelope. Same container walk as the Tab5 firmware's pocket_system. */
static bool variant_section(const uint8_t *bytes, size_t size,
                            const pocket_idf_package_facts *facts, uint32_t kind,
                            const uint8_t **payload, size_t *length) {
  enum { HEADER = 32, VARIANT = 64, SECTION = 16, FOOTER = 8 };
  if (size < HEADER + FOOTER)
    return false;
  const uint64_t payload_end = size - FOOTER;
#define WITHIN(offset, count) \
  ((uint64_t)(offset) <= payload_end && (uint64_t)(count) <= payload_end - (offset))
  const uint64_t table = HEADER + ((uint64_t)le32(bytes + 8) + 15) / 16 * 16;
  const uint32_t variants = le32(bytes + 12);
  if (variants > 64 || !WITHIN(table, (uint64_t)variants * VARIANT))
    return false;
  for (uint32_t v = 0; v < variants; ++v) {
    const uint8_t *variant = bytes + table + (uint64_t)v * VARIANT;
    if (le32(variant + 8) != pocket_profile.host_abi ||
        le64(variant + 24) != facts->variant_hash)
      continue;
    const uint32_t count = le32(variant + 12), sections = le32(variant + 16);
    if (count > 64 || !WITHIN(sections, (uint64_t)count * SECTION))
      return false;
    for (uint32_t s = 0; s < count; ++s) {
      const uint8_t *section = bytes + sections + (uint64_t)s * SECTION;
      const uint32_t start = le32(section + 8), span = le32(section + 12);
      if (le32(section) != kind)
        continue;
      if (span == 0 || !WITHIN(start, span))
        return false;
      *payload = bytes + start;
      *length = span;
      return true;
    }
    return false;
  }
#undef WITHIN
  return false;
}

bool tdeck_app_worker(const tdeck_app_t *app, const char **source, size_t *size) {
  const uint8_t *payload = NULL;
  size_t span = 0;
  if (!app || !variant_section(app->bytes, app->size, &app->facts, 8, &payload, &span) ||
      span < 2 || payload[span - 1] != 0)
    return false;
  *source = (const char *)payload;
  *size = span - 1;
  return true;
}

bool tdeck_app_worker_bytecode(const tdeck_app_t *app, const uint8_t **envelope, size_t *size) {
  return app && variant_section(app->bytes, app->size, &app->facts, 9, envelope, size);
}

bool tdeck_switch_pending(void) { return launcher_pending(); }

bool tdeck_take_switch(char slot[17], bool *launcher) {
  LauncherTarget target;
  if (!launcher_take_request(&target))
    return false;
  memcpy(slot, target.slot, 17);
  *launcher = target.home;
  return true;
}

void tdeck_summon(void) { launcher_summon(); }

void tdeck_report_failure(const char *message) {
  launcher_report_failure(launcher_current_output(), message);
}

const char *tdeck_app_table(void) { return launcher_table_json(); }
bool tdeck_launch(const char *output) { return launcher_launch(output); }

/* ---- data.fs ---------------------------------------------------------------- */

enum {
  OP_FS_READ,
  OP_FS_WRITE,
  OP_FS_REMOVE,
  OP_FS_LIST,
  OP_FS_STAT,
  OP_FS_MKDIR,
  OP_FS_RENAME,
  OP_FS_USAGE,
  OP_FS_LAST_ERROR,
};

static const char *text_arg(JSContext *ctx, int argc, JSValueConst *argv, int i, size_t *length) {
  *length = 0;
  if (i >= argc || !JS_IsString(argv[i]))
    return NULL;
  return JS_ToCStringLen(ctx, length, argv[i]);
}

static double float_arg(JSContext *ctx, int argc, JSValueConst *argv, int i) {
  double value = 0;
  if (i < argc)
    JS_ToFloat64(ctx, &value, argv[i]);
  return value;
}

static int32_t int_arg(JSContext *ctx, int argc, JSValueConst *argv, int i) {
  int32_t value = 0;
  if (i < argc)
    JS_ToInt32(ctx, &value, argv[i]);
  return value;
}

static JSValue line_result(JSContext *ctx, const char *line) {
  return JS_NewString(ctx, line ? line : "{\"error\":\"out of memory\"}");
}

static JSValue fs_op_timed(JSContext *ctx, JSValueConst this_value, int argc,
                           JSValueConst *argv, int magic);

static JSValue fs_op(JSContext *ctx, JSValueConst this_value, int argc,
                     JSValueConst *argv, int magic) {
  static const char *const names[] = {"read", "write", "remove", "list", "stat",
                                      "mkdir", "rename", "usage", "lastError"};
  const int64_t started = esp_timer_get_time();
  JSValue result = fs_op_timed(ctx, this_value, argc, argv, magic);
  const int64_t ms = (esp_timer_get_time() - started) / 1000;
  if (ms >= 25)
    ESP_LOGW(TAG, "fs.%s took %lld ms", names[magic], (long long)ms);
  return result;
}

static JSValue fs_op_timed(JSContext *ctx, JSValueConst this_value, int argc,
                           JSValueConst *argv, int magic) {
  (void)this_value;
  size_t length = 0, second_length = 0;
  const char *text = NULL, *second = NULL;
  JSValue result = JS_UNDEFINED;
  switch (magic) {
  case OP_FS_READ:
    text = text_arg(ctx, argc, argv, 0, &length);
    result = line_result(ctx, fs_read(text, length, float_arg(ctx, argc, argv, 1),
                                      float_arg(ctx, argc, argv, 2)));
    break;
  case OP_FS_LIST:
    text = text_arg(ctx, argc, argv, 0, &length);
    result = line_result(ctx, fs_list(text, length, float_arg(ctx, argc, argv, 1)));
    break;
  case OP_FS_STAT:
    text = text_arg(ctx, argc, argv, 0, &length);
    result = line_result(ctx, fs_stat(text, length));
    break;
  case OP_FS_WRITE:
    text = text_arg(ctx, argc, argv, 0, &length);
    second = text_arg(ctx, argc, argv, 1, &second_length);
    result = JS_NewInt32(ctx, fs_write(text, length, second, second_length,
                                       int_arg(ctx, argc, argv, 2)));
    break;
  case OP_FS_REMOVE:
    text = text_arg(ctx, argc, argv, 0, &length);
    result = JS_NewInt32(ctx, fs_remove(text, length, int_arg(ctx, argc, argv, 1)));
    break;
  case OP_FS_MKDIR:
    text = text_arg(ctx, argc, argv, 0, &length);
    result = JS_NewInt32(ctx, fs_mkdir(text, length));
    break;
  case OP_FS_RENAME:
    text = text_arg(ctx, argc, argv, 0, &length);
    second = text_arg(ctx, argc, argv, 1, &second_length);
    result = JS_NewInt32(ctx, fs_rename(text, length, second, second_length));
    break;
  case OP_FS_USAGE:
    result = line_result(ctx, fs_usage());
    break;
  case OP_FS_LAST_ERROR:
    result = JS_NewString(ctx, fs_last_error());
    break;
  default:
    break;
  }
  if (text)
    JS_FreeCString(ctx, text);
  if (second)
    JS_FreeCString(ctx, second);
  return result;
}

static void add(JSContext *ctx, JSValue object, const char *name, int arity, int magic) {
  JS_SetPropertyStr(ctx, object, name,
                    JS_NewCFunctionMagic(ctx, fs_op, name, arity, JS_CFUNC_generic_magic, magic));
}

esp_err_t tdeck_bind(JSContext *realm, const tdeck_app_t *app) {
  LauncherPackage package = package_of(app);
  launcher_set_current(app->facts.slot, &package);
  if (!(app->facts.features & POCKET_IDF_FEATURE_FS))
    return ESP_OK;
  if (!mounted) {
    ESP_LOGW(TAG, "data.fs requested but no SD card");
    return ESP_OK;
  }
  char root[96], temp[96], slot_dir[80];
  launcher_host_slot_path(app->facts.slot, NULL, slot_dir, sizeof slot_dir);
  make_dir(slot_dir);
  launcher_host_slot_path(app->facts.slot, "data", root, sizeof root);
  launcher_host_slot_path(app->facts.slot, "fs-tmp", temp, sizeof temp);
  make_dir(root);
  make_dir(temp);
  if (!fs_bind(root, temp, fs_plan_quota((const char *)app->facts.plan, app->facts.plan_length))) {
    ESP_LOGE(TAG, "fs_bind failed: %s", fs_last_error());
    return ESP_FAIL;
  }
  JSValue global = JS_GetGlobalObject(realm);
  JSValue fs = JS_NewObject(realm);
  add(realm, fs, "read", 3, OP_FS_READ);
  add(realm, fs, "write", 3, OP_FS_WRITE);
  add(realm, fs, "remove", 2, OP_FS_REMOVE);
  add(realm, fs, "list", 2, OP_FS_LIST);
  add(realm, fs, "stat", 1, OP_FS_STAT);
  add(realm, fs, "mkdir", 1, OP_FS_MKDIR);
  add(realm, fs, "rename", 2, OP_FS_RENAME);
  add(realm, fs, "usage", 0, OP_FS_USAGE);
  add(realm, fs, "lastError", 0, OP_FS_LAST_ERROR);
  JS_SetPropertyStr(realm, global, "fs", fs);
  bool failed = JS_HasException(realm);
  JS_FreeValue(realm, global);
  return failed ? ESP_ERR_NO_MEM : ESP_OK;
}

void tdeck_unbind(void) { fs_unbind(); }
