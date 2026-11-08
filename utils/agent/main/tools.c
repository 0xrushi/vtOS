/* SPDX-License-Identifier: MIT */
#include "tools.h"

#include "esp_log.h"
#include "esp_spiffs.h"

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>

#define TAG "tools"
#define WORK "/data"

static bool mounted_;

bool tools_init(void)
{
    const esp_vfs_spiffs_conf_t conf = {
        .base_path = WORK,
        .partition_label = "agentfs",
        .max_files = 4,
        .format_if_mount_failed = true,
    };
    esp_err_t err = esp_vfs_spiffs_register(&conf);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "storage: %s", esp_err_to_name(err));
        return false;
    }
    mounted_ = true;
    return true;
}

bool tool_name_ok(const char *name)
{
    size_t n = name ? strlen(name) : 0;
    if (n == 0 || n > 31 || name[0] == '.') {
        return false;
    }
    for (size_t i = 0; i < n; ++i) {
        char c = name[i];
        bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-';
        if (!ok) {
            return false;
        }
    }
    return strstr(name, "..") == NULL;
}

static int path_of(const char *name, char *path, size_t cap)
{
    if (!mounted_) {
        return -ENODEV;
    }
    if (!tool_name_ok(name)) {
        return -EINVAL;
    }
    int n = snprintf(path, cap, WORK "/%s", name);
    return (n > 0 && (size_t)n < cap) ? 0 : -ENAMETOOLONG;
}

int tool_read(const char *name, char *out, size_t cap)
{
    char path[320];
    int err = path_of(name, path, sizeof path);
    if (err) {
        return err;
    }
    FILE *file = fopen(path, "rb");
    if (!file) {
        return -ENOENT;
    }
    size_t cap_data = cap - 1 < TOOL_MAX_BYTES ? cap - 1 : TOOL_MAX_BYTES;
    size_t got = fread(out, 1, cap_data, file);
    fclose(file);
    out[got] = '\0';
    return (int)got;
}

int tool_write(const char *name, const char *data, size_t length)
{
    char path[320];
    int err = path_of(name, path, sizeof path);
    if (err) {
        return err;
    }
    if (length > TOOL_MAX_BYTES) {
        return -EFBIG;
    }
    FILE *file = fopen(path, "wb");
    if (!file) {
        return -EIO;
    }
    size_t put = fwrite(data, 1, length, file);
    bool ok = fclose(file) == 0 && put == length;
    return ok ? (int)put : -EIO;
}

int tool_list(void (*emit)(const char *name, long size))
{
    if (!mounted_) {
        return -ENODEV;
    }
    DIR *dir = opendir(WORK);
    if (!dir) {
        return -ENOENT;
    }
    int count = 0;
    struct dirent *entry;
    while ((entry = readdir(dir)) != NULL) {
        char path[320];
        struct stat st;
        snprintf(path, sizeof path, WORK "/%s", entry->d_name);
        long size = stat(path, &st) == 0 ? (long)st.st_size : -1;
        emit(entry->d_name, size);
        count++;
    }
    closedir(dir);
    return count;
}

static const char *ci_find(const char *hay, const char *needle)
{
    size_t n = strlen(needle);
    if (n == 0) {
        return hay;
    }
    for (; *hay; ++hay) {
        size_t i = 0;
        while (i < n && hay[i] && tolower((unsigned char)hay[i]) == tolower((unsigned char)needle[i])) {
            i++;
        }
        if (i == n) {
            return hay;
        }
    }
    return NULL;
}

static int grep_one(const char *name, const char *pattern, char *out, size_t cap,
                    int max_matches, int found, bool prefix)
{
    char path[320];
    if (path_of(name, path, sizeof path) != 0) {
        return found;
    }
    FILE *file = fopen(path, "r");
    if (!file) {
        return found;
    }
    char line[256];
    int lineno = 0;
    size_t used = strlen(out);
    while (fgets(line, sizeof line, file) && found < max_matches) {
        lineno++;
        line[strcspn(line, "\r\n")] = '\0';
        if (!ci_find(line, pattern)) {
            continue;
        }
        if (strlen(line) > 100) {
            line[100] = '\0';
        }
        int n = prefix ? snprintf(out + used, cap - used, "%s:%d: %s\n", name, lineno, line)
                       : snprintf(out + used, cap - used, "%d: %s\n", lineno, line);
        if (n < 0 || (size_t)n >= cap - used) {
            break;
        }
        used += (size_t)n;
        found++;
    }
    fclose(file);
    return found;
}

int tool_grep(const char *name, const char *pattern, char *out, size_t cap, int max_matches)
{
    if (!mounted_) {
        return -ENODEV;
    }
    if (!pattern || !pattern[0]) {
        return -EINVAL;
    }
    out[0] = '\0';
    if (name && name[0]) {
        if (!tool_name_ok(name)) {
            return -EINVAL;
        }
        return grep_one(name, pattern, out, cap, max_matches, 0, false);
    }
    DIR *dir = opendir(WORK);
    if (!dir) {
        return -ENOENT;
    }
    int found = 0;
    struct dirent *entry;
    while ((entry = readdir(dir)) != NULL && found < max_matches) {
        if (tool_name_ok(entry->d_name)) {
            found = grep_one(entry->d_name, pattern, out, cap, max_matches, found, true);
        }
    }
    closedir(dir);
    return found;
}
