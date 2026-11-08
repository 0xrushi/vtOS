#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

// Keep incomplete stereo frames separate from the valid samples. The next
// capture read prepends carry before appending more I2S bytes to chunk.
static inline size_t vtos_capture_pack(uint8_t *chunk, size_t total,
                                        uint8_t carry[4], size_t *carry_len,
                                        bool mono) {
  size_t usable = total - total % 4;
  *carry_len = total - usable;
  memcpy(carry, chunk + usable, *carry_len);
  if (!mono) {
    return usable;
  }
  for (size_t i = 0; i < usable / 4; i++) {
    chunk[i * 2] = chunk[i * 4];
    chunk[i * 2 + 1] = chunk[i * 4 + 1];
  }
  return usable / 2;
}
