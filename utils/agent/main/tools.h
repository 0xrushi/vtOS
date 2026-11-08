/* SPDX-License-Identifier: MIT
 * The agent's only two tools, sandboxed to the internal /data partition: read_file and
 * write_file (plus ls for the user). Flat namespace: names are [A-Za-z0-9._-],
 * at most 31 bytes, never starting with '.'; no directories, no "..", no
 * absolute paths, no delete.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#define TOOL_MAX_BYTES 8192

/* Mount the internal "agentfs" SPIFFS partition at /data (formats it the
 * first time). Flat namespace. False when the partition is missing. */
bool tools_init(void);
/* The name is acceptable inside the sandbox. */
bool tool_name_ok(const char *name);
/* Bytes read (<= cap-1, NUL-terminated), or a negative error. */
int tool_read(const char *name, char *out, size_t cap);
/* Bytes written, or a negative error. Truncates an existing file. */
int tool_write(const char *name, const char *data, size_t length);
/* Case-insensitive substring search. `name` empty = every file in the folder.
 * Writes "file:line: text" lines (text cut to 100 chars) into `out`, at most
 * `max_matches` of them; streams each file, never loads it whole. Returns the
 * match count or a negative error. */
int tool_grep(const char *name, const char *pattern, char *out, size_t cap, int max_matches);
/* Call `emit` for every file; returns the count. */
int tool_list(void (*emit)(const char *name, long size));
