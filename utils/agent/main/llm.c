/* SPDX-License-Identifier: MIT */
#include "llm.h"

#include "console.h"
#include "display_min.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "net.h"
#include "tools.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define TAG "llm"

#define HIST_BYTES 12288
#define BODY_BYTES 16384
#define ARGS_MAX 6000
#define TEXT_MAX 4096
#define MAX_CALLS 3
#define MAX_STEPS 8
#define LINE_MAX 2560
#define SYSTEM_PROMPT \
    "You are a helpful assistant on a handheld with a 40x30 character screen. " \
    "Reply briefly in plain text, no markdown. Tools work on one flat folder " \
    "of text files: list_files, read_file, grep_file, write_file. Look with " \
    "the tools before answering about files. Write only what the user asks."

#define TOOLS_JSON \
    "[{\"type\":\"function\",\"function\":{\"name\":\"list_files\"," \
    "\"description\":\"List files with sizes.\",\"parameters\":{\"type\":\"object\"," \
    "\"properties\":{}}}}," \
    "{\"type\":\"function\",\"function\":{\"name\":\"read_file\"," \
    "\"description\":\"Read a text file (first 3000 bytes).\"," \
    "\"parameters\":{\"type\":\"object\",\"properties\":{\"name\":{\"type\":\"string\"}}," \
    "\"required\":[\"name\"]}}}," \
    "{\"type\":\"function\",\"function\":{\"name\":\"grep_file\"," \
    "\"description\":\"Find lines containing text (case-insensitive). Omit name to search all files.\"," \
    "\"parameters\":{\"type\":\"object\",\"properties\":{\"pattern\":{\"type\":\"string\"}," \
    "\"name\":{\"type\":\"string\"}},\"required\":[\"pattern\"]}}}," \
    "{\"type\":\"function\",\"function\":{\"name\":\"write_file\"," \
    "\"description\":\"Create or overwrite a text file (user confirms).\"," \
    "\"parameters\":{\"type\":\"object\",\"properties\":{\"name\":{\"type\":\"string\"}," \
    "\"content\":{\"type\":\"string\"}},\"required\":[\"name\",\"content\"]}}}]"

static agent_config_t cfg_;
static llm_confirm_fn confirm_;

/* ---- config ----------------------------------------------------------------- */

bool config_load(agent_config_t *c)
{
    memset(c, 0, sizeof *c);
    FILE *f = fopen("/data/config.txt", "r");
    if (!f) {
        return false;
    }
    char line[220];
    while (fgets(line, sizeof line, f)) {
        line[strcspn(line, "\r\n")] = '\0';
        char *eq = strchr(line, '=');
        if (!eq || line[0] == '#') {
            continue;
        }
        *eq = '\0';
        const char *key = line, *val = eq + 1;
        if (!strcmp(key, "wifi_ssid")) strlcpy(c->wifi_ssid, val, sizeof c->wifi_ssid);
        else if (!strcmp(key, "wifi_pass")) strlcpy(c->wifi_pass, val, sizeof c->wifi_pass);
        else if (!strcmp(key, "api_base")) strlcpy(c->api_base, val, sizeof c->api_base);
        else if (!strcmp(key, "api_key")) strlcpy(c->api_key, val, sizeof c->api_key);
        else if (!strcmp(key, "model")) strlcpy(c->model, val, sizeof c->model);
        else if (!strcmp(key, "assemblyai_key")) strlcpy(c->aai_key, val, sizeof c->aai_key);
    }
    fclose(f);
    return true;
}

bool config_save(const agent_config_t *c)
{
    FILE *f = fopen("/data/config.txt", "w");
    if (!f) {
        return false;
    }
    fprintf(f, "wifi_ssid=%s\nwifi_pass=%s\napi_base=%s\napi_key=%s\nmodel=%s\nassemblyai_key=%s\n",
            c->wifi_ssid, c->wifi_pass, c->api_base, c->api_key, c->model, c->aai_key);
    return fclose(f) == 0;
}

bool config_set(agent_config_t *c, const char *key, const char *value)
{
    char *dst = NULL;
    size_t cap = 0;
    if (!strcmp(key, "wifi_ssid")) { dst = c->wifi_ssid; cap = sizeof c->wifi_ssid; }
    else if (!strcmp(key, "wifi_pass")) { dst = c->wifi_pass; cap = sizeof c->wifi_pass; }
    else if (!strcmp(key, "api_base")) { dst = c->api_base; cap = sizeof c->api_base; }
    else if (!strcmp(key, "api_key")) { dst = c->api_key; cap = sizeof c->api_key; }
    else if (!strcmp(key, "model")) { dst = c->model; cap = sizeof c->model; }
    else if (!strcmp(key, "assemblyai_key")) { dst = c->aai_key; cap = sizeof c->aai_key; }
    if (!dst) {
        return false;
    }
    strlcpy(dst, value, cap);
    return true;
}

void llm_init(const agent_config_t *config, llm_confirm_fn confirm)
{
    cfg_ = *config;
    confirm_ = confirm;
}

/* ---- tiny JSON helpers ------------------------------------------------------- */

/* Escape `src` as the inside of a JSON string; returns bytes written (no NUL
 * counted), stops before overflowing `cap`. */
static size_t esc(char *dst, size_t cap, const char *src)
{
    size_t n = 0;
    for (; *src && n + 7 < cap; ++src) {
        unsigned char c = (unsigned char)*src;
        if (c == '"' || c == '\\') {
            dst[n++] = '\\';
            dst[n++] = (char)c;
        } else if (c == '\n') {
            dst[n++] = '\\';
            dst[n++] = 'n';
        } else if (c == '\r') {
            dst[n++] = '\\';
            dst[n++] = 'r';
        } else if (c == '\t') {
            dst[n++] = '\\';
            dst[n++] = 't';
        } else if (c < 32) {
            n += (size_t)snprintf(dst + n, cap - n, "\\u%04x", c);
        } else {
            dst[n++] = (char)c;
        }
    }
    dst[n] = '\0';
    return n;
}

/* Decode a JSON string body starting at `p` (just after the opening quote)
 * into `out`; returns the pointer after the closing quote, or NULL. */
static const char *unesc(const char *p, char *out, size_t cap)
{
    size_t n = 0;
    while (*p && *p != '"') {
        char c = *p++;
        if (c == '\\' && *p) {
            char e = *p++;
            switch (e) {
            case 'n': c = '\n'; break;
            case 't': c = '\t'; break;
            case 'r': c = '\r'; break;
            case 'b': case 'f': c = ' '; break;
            case 'u': {
                unsigned v = 0;
                for (int i = 0; i < 4 && *p; ++i, ++p) {
                    char h = *p;
                    v = v * 16 + (unsigned)(h <= '9' ? h - '0' : (h | 32) - 'a' + 10);
                }
                c = v < 128 ? (char)v : '?';
                break;
            }
            default: c = e; break;
            }
        }
        if (n + 1 < cap) {
            out[n++] = c;
        }
    }
    out[n] = '\0';
    return *p == '"' ? p + 1 : NULL;
}

/* "key":"value" -> value (decoded). */
static bool json_str(const char *json, const char *key, char *out, size_t cap)
{
    char pat[40];
    snprintf(pat, sizeof pat, "\"%s\":\"", key);
    const char *p = strstr(json, pat);
    if (!p) {
        return false;
    }
    return unesc(p + strlen(pat), out, cap) != NULL;
}

/* ---- history: JSON messages separated by NUL ------------------------------------ */

static char hist_[HIST_BYTES];
static size_t hist_len_;

static void hist_drop_first(void)
{
    size_t n = strlen(hist_) + 1;
    memmove(hist_, hist_ + n, hist_len_ - n);
    hist_len_ -= n;
}

/* The oldest message must be a user turn, or the API rejects an orphan tool
 * result. */
static void hist_fix_head(void)
{
    while (hist_len_ && strncmp(hist_, "{\"role\":\"user\"", 14) != 0) {
        hist_drop_first();
    }
}

static void hist_add(const char *json)
{
    size_t n = strlen(json) + 1;
    if (n > HIST_BYTES) {
        return;
    }
    while (hist_len_ + n > HIST_BYTES && hist_len_) {
        hist_drop_first();
    }
    memcpy(hist_ + hist_len_, json, n);
    hist_len_ += n;
    if (hist_len_ > 0 && strncmp(hist_, "{\"role\":\"user\"", 14) != 0) {
        hist_fix_head();
    }
}

void llm_reset(void)
{
    hist_len_ = 0;
}

/* ---- console output buffering ---------------------------------------------------- */

static char obuf_[96];
static size_t olen_;

static void out_flush(void)
{
    if (olen_) {
        obuf_[olen_] = '\0';
        con_print(COLOR_WHITE, obuf_);
        olen_ = 0;
    }
}

static void out_text(const char *s)
{
    for (; *s; ++s) {
        obuf_[olen_++] = *s;
        if (*s == '\n' || olen_ >= 24) {
            out_flush();
        }
    }
}

/* ---- one streamed completion --------------------------------------------------------- */

typedef struct {
    char id[48];
    char name[24];
    char *args; /* ARGS_MAX, decoded JSON text of the arguments */
    size_t args_len;
} call_t;

typedef struct {
    char *text; /* TEXT_MAX: assistant content so far */
    size_t text_len;
    call_t calls[MAX_CALLS];
    int ncalls;
    char finish[24];
    bool done;
} turn_t;

static call_t *call_for(turn_t *t, int index)
{
    if (index < 0 || index >= MAX_CALLS) {
        return NULL;
    }
    if (index >= t->ncalls) {
        t->ncalls = index + 1;
    }
    return &t->calls[index];
}

static void append_decoded(char *dst, size_t *len, size_t cap, const char *src)
{
    char tmp[600];
    unesc(src, tmp, sizeof tmp);
    size_t n = strlen(tmp);
    if (*len + n + 1 > cap) {
        n = cap - *len - 1;
    }
    memcpy(dst + *len, tmp, n);
    *len += n;
    dst[*len] = '\0';
}

/* One SSE line: data: {...} */
static void on_line(turn_t *t, const char *line)
{
    if (strncmp(line, "data:", 5) != 0) {
        return;
    }
    line += 5;
    while (*line == ' ') {
        line++;
    }
    if (!strncmp(line, "[DONE]", 6)) {
        t->done = true;
        return;
    }
    const char *delta = strstr(line, "\"delta\":");
    const char *scan = delta ? delta : line;

    const char *tc = strstr(scan, "\"tool_calls\"");
    if (tc) {
        const char *ip = strstr(tc, "\"index\":");
        int index = ip ? atoi(ip + 8) : 0;
        call_t *c = call_for(t, index);
        if (c) {
            const char *idp = strstr(tc, "\"id\":\"");
            if (idp) {
                unesc(idp + 6, c->id, sizeof c->id);
            }
            const char *np = strstr(tc, "\"name\":\"");
            if (np) {
                unesc(np + 8, c->name, sizeof c->name);
            }
            const char *ap = strstr(tc, "\"arguments\":\"");
            if (ap && c->args) {
                append_decoded(c->args, &c->args_len, ARGS_MAX, ap + 13);
            }
        }
    } else {
        const char *cp = strstr(scan, "\"content\":\"");
        if (cp) {
            char piece[600];
            unesc(cp + 11, piece, sizeof piece);
            if (piece[0]) {
                out_text(piece);
                size_t n = strlen(piece);
                if (t->text_len + n + 1 <= TEXT_MAX) {
                    memcpy(t->text + t->text_len, piece, n + 1);
                    t->text_len += n;
                }
            }
        }
    }
    const char *fp = strstr(line, "\"finish_reason\":\"");
    if (fp) {
        unesc(fp + 17, t->finish, sizeof t->finish);
    }
}

static bool stream_turn(turn_t *t)
{
    char *body = heap_caps_malloc(BODY_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!body) {
        con_print(COLOR_RED, "out of memory\n");
        return false;
    }
    size_t n = (size_t)snprintf(body, BODY_BYTES,
                                "{\"model\":\"%s\",\"stream\":true,\"messages\":[{\"role\":\"system\","
                                "\"content\":\"%s\"}",
                                cfg_.model, SYSTEM_PROMPT);
    for (size_t off = 0; off < hist_len_;) {
        size_t m = strlen(hist_ + off);
        if (n + m + 2 + sizeof TOOLS_JSON + 64 > BODY_BYTES) {
            break;
        }
        body[n++] = ',';
        memcpy(body + n, hist_ + off, m);
        n += m;
        off += m + 1;
    }
    n += (size_t)snprintf(body + n, BODY_BYTES - n,
                          "],\"tools\":%s,\"tool_choice\":\"auto\"}", TOOLS_JSON);

    char url[160];
    snprintf(url, sizeof url, "%s/chat/completions", cfg_.api_base);
    esp_http_client_config_t hc = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .crt_bundle_attach = esp_crt_bundle_attach,
        .timeout_ms = 45000,
        .buffer_size = 1024,
        .buffer_size_tx = 1024,
    };
    esp_http_client_handle_t client = esp_http_client_init(&hc);
    bool ok = false;
    char auth[160];
    snprintf(auth, sizeof auth, "Bearer %s", cfg_.api_key);
    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_header(client, "Authorization", auth);
    esp_http_client_set_header(client, "Accept", "text/event-stream");

    esp_err_t err = esp_http_client_open(client, (int)n);
    if (err != ESP_OK) {
        con_printf(COLOR_RED, "connect failed: %s\n", esp_err_to_name(err));
        goto out;
    }
    if (esp_http_client_write(client, body, (int)n) < 0) {
        con_print(COLOR_RED, "send failed\n");
        goto out;
    }
    esp_http_client_fetch_headers(client);
    int status = esp_http_client_get_status_code(client);
    char *line = heap_caps_malloc(LINE_MAX, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!line) {
        goto out;
    }
    size_t ll = 0;
    char chunk[512];
    if (status != 200) {
        int got = esp_http_client_read(client, chunk, sizeof chunk - 1);
        chunk[got > 0 ? got : 0] = '\0';
        char msg[160] = "";
        if (!json_str(chunk, "message", msg, sizeof msg)) {
            strlcpy(msg, chunk, sizeof msg);
        }
        con_printf(COLOR_RED, "API error %d: %s\n", status, msg);
        heap_caps_free(line);
        goto out;
    }
    for (;;) {
        int got = esp_http_client_read(client, chunk, sizeof chunk);
        if (got <= 0) {
            break;
        }
        for (int i = 0; i < got; ++i) {
            if (chunk[i] == '\n') {
                line[ll] = '\0';
                if (ll) {
                    on_line(t, line);
                }
                ll = 0;
            } else if (ll + 1 < LINE_MAX && chunk[i] != '\r') {
                line[ll++] = chunk[i];
            }
        }
        if (t->done) {
            break;
        }
    }
    heap_caps_free(line);
    out_flush();
    ok = true;
out:
    esp_http_client_cleanup(client);
    heap_caps_free(body);
    return ok;
}

/* ---- tools ------------------------------------------------------------------------------ */

static char list_acc_[1500];

static void list_emit(const char *name, long size)
{
    size_t used = strlen(list_acc_);
    snprintf(list_acc_ + used, sizeof list_acc_ - used, "%s %ld\n", name, size);
}

static void run_tool(const call_t *c, char *result, size_t cap)
{
    char name[64] = "";
    char pattern[100] = "";
    bool have_name = c->args_len && json_str(c->args, "name", name, sizeof name);
    if (!strcmp(c->name, "list_files")) {
        con_print(COLOR_DIM, "[list]\n");
        list_acc_[0] = '\0';
        int n = tool_list(list_emit);
        if (n < 0) {
            snprintf(result, cap, "error: cannot list files");
        } else {
            snprintf(result, cap, "%s", n ? list_acc_ : "(no files)");
        }
    } else if (!strcmp(c->name, "grep_file")) {
        if (!c->args_len || !json_str(c->args, "pattern", pattern, sizeof pattern)) {
            snprintf(result, cap, "error: missing pattern");
            return;
        }
        con_printf(COLOR_DIM, "[grep %s%s%s]\n", pattern, have_name ? " in " : "", have_name ? name : "");
        int n = tool_grep(have_name ? name : "", pattern, result, cap, 20);
        if (n < 0) {
            snprintf(result, cap, "error: cannot search");
        } else if (n == 0) {
            snprintf(result, cap, "no matches");
        }
    } else if (!strcmp(c->name, "read_file")) {
        if (!have_name) {
            snprintf(result, cap, "error: missing name");
            return;
        }
        con_printf(COLOR_DIM, "[read %s]\n", name);
        static char buf[3001];
        int got = tool_read(name, buf, sizeof buf);
        if (got < 0) {
            snprintf(result, cap, "error: cannot read %s", name);
        } else {
            snprintf(result, cap, "%s", buf);
        }
    } else if (!strcmp(c->name, "write_file")) {
        if (!have_name) {
            snprintf(result, cap, "error: missing name");
            return;
        }
        char *content = heap_caps_malloc(ARGS_MAX, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (!content || !json_str(c->args, "content", content, ARGS_MAX)) {
            snprintf(result, cap, "error: missing content (maybe too long)");
            heap_caps_free(content);
            return;
        }
        char prompt[96];
        snprintf(prompt, sizeof prompt, "write %s (%u bytes)? y/n", name, (unsigned)strlen(content));
        if (!tool_name_ok(name)) {
            snprintf(result, cap, "error: bad file name");
        } else if (confirm_ && !confirm_(prompt)) {
            snprintf(result, cap, "declined by the user");
        } else {
            int put = tool_write(name, content, strlen(content));
            if (put < 0) {
                snprintf(result, cap, "error: write failed");
            } else {
                snprintf(result, cap, "ok: wrote %d bytes to %s", put, name);
                con_printf(COLOR_GREEN, "[wrote %s]\n", name);
            }
        }
        heap_caps_free(content);
    } else {
        snprintf(result, cap, "error: unknown tool %s", c->name);
    }
}

/* ---- the agent loop ------------------------------------------------------------------------ */

void llm_chat(const char *user_text)
{
    if (!cfg_.api_base[0] || !cfg_.api_key[0] || !cfg_.model[0]) {
        con_print(COLOR_RED, "no model: set them with /set (see /help)\n");
        return;
    }
    if (!net_wait_online(8000)) {
        con_print(COLOR_RED, "Wi-Fi is not connected\n");
        return;
    }
    net_wait_clock(8000);

    char *tmp = heap_caps_malloc(ARGS_MAX * 2 + 512, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!tmp) {
        return;
    }
    size_t n = (size_t)snprintf(tmp, 64, "{\"role\":\"user\",\"content\":\"");
    n += esc(tmp + n, 3000, user_text);
    snprintf(tmp + n, 8, "\"}");
    hist_add(tmp);

    turn_t *t = heap_caps_calloc(1, sizeof *t, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    t->text = heap_caps_malloc(TEXT_MAX, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    for (int i = 0; i < MAX_CALLS; ++i) {
        t->calls[i].args = heap_caps_malloc(ARGS_MAX, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    }
    for (int step = 0; step < MAX_STEPS; ++step) {
        t->text_len = 0;
        t->text[0] = '\0';
        t->ncalls = 0;
        t->finish[0] = '\0';
        t->done = false;
        for (int i = 0; i < MAX_CALLS; ++i) {
            t->calls[i].id[0] = t->calls[i].name[0] = '\0';
            t->calls[i].args_len = 0;
            t->calls[i].args[0] = '\0';
        }
        if (!stream_turn(t)) {
            break;
        }
        if (t->ncalls == 0) {
            if (t->text_len) {
                size_t m = (size_t)snprintf(tmp, 40, "{\"role\":\"assistant\",\"content\":\"");
                m += esc(tmp + m, 3000, t->text);
                snprintf(tmp + m, 8, "\"}");
                hist_add(tmp);
            }
            con_print(COLOR_WHITE, "\n");
            break;
        }
        /* assistant message with its tool calls */
        size_t m = (size_t)snprintf(tmp, 64, "{\"role\":\"assistant\",\"content\":\"");
        m += esc(tmp + m, 1200, t->text);
        m += (size_t)snprintf(tmp + m, 40, "\",\"tool_calls\":[");
        for (int i = 0; i < t->ncalls; ++i) {
            m += (size_t)snprintf(tmp + m, 160,
                                  "%s{\"id\":\"%s\",\"type\":\"function\",\"function\":{\"name\":\"%s\",\"arguments\":\"",
                                  i ? "," : "", t->calls[i].id, t->calls[i].name);
            m += esc(tmp + m, ARGS_MAX + 256, t->calls[i].args);
            m += (size_t)snprintf(tmp + m, 8, "\"}}");
        }
        snprintf(tmp + m, 8, "]}");
        hist_add(tmp);
        /* one tool message per call */
        for (int i = 0; i < t->ncalls; ++i) {
            static char result[3100];
            run_tool(&t->calls[i], result, sizeof result);
            size_t k = (size_t)snprintf(tmp, 200, "{\"role\":\"tool\",\"tool_call_id\":\"%s\",\"content\":\"",
                                        t->calls[i].id);
            k += esc(tmp + k, 6200, result);
            snprintf(tmp + k, 8, "\"}");
            hist_add(tmp);
        }
    }
    for (int i = 0; i < MAX_CALLS; ++i) {
        heap_caps_free(t->calls[i].args);
    }
    heap_caps_free(t->text);
    heap_caps_free(t);
    heap_caps_free(tmp);
}
