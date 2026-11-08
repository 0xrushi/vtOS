/* SPDX-License-Identifier: MIT
 * OpenAI-compatible chat agent: streamed /chat/completions, two tools
 * (read_file, write_file), history kept as pre-serialised JSON in a 12 KB ring.
 */
#pragma once

#include <stdbool.h>

typedef struct {
    char wifi_ssid[33];
    char wifi_pass[65];
    char api_base[96];  /* e.g. https://api.openai.com/v1 */
    char api_key[128];
    char model[48];
    char aai_key[64];   /* AssemblyAI, phase A4 */
} agent_config_t;

/* Config lives in the internal /data partition as key=value lines. */
bool config_load(agent_config_t *config);
bool config_save(const agent_config_t *config);
/* Set one field by name (wifi_ssid wifi_pass api_base api_key model
 * assemblyai_key); false for an unknown key. */
bool config_set(agent_config_t *config, const char *key, const char *value);

/* Called to ask the user yes/no on the console; blocks. */
typedef bool (*llm_confirm_fn)(const char *prompt);

void llm_init(const agent_config_t *config, llm_confirm_fn confirm);
/* Send one user message and run the tool loop to completion; prints to the
 * console as tokens stream in. Blocking. */
void llm_chat(const char *user_text);
void llm_reset(void);
