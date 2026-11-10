// RFB pixel conversion and Hextile encoding for the shadow framebuffer.
#ifndef VTOS_RFB_H
#define VTOS_RFB_H

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

typedef struct {
    int bytes;
    bool big_endian;
    uint16_t max[3];
    uint8_t shift[3];
} rfb_format_t;

static uint16_t rfb_read565(const uint8_t *src) {
    return ((uint16_t)src[0] << 8) | src[1];
}

static uint8_t *rfb_write_pixel(uint8_t *dst, uint16_t pixel, const rfb_format_t *fmt) {
    uint32_t r = pixel >> 11;
    uint32_t g = (pixel >> 5) & 63;
    uint32_t b = pixel & 31;
    r = (r << 3) | (r >> 2);
    g = (g << 2) | (g >> 4);
    b = (b << 3) | (b >> 2);
    uint32_t value = (((r * fmt->max[0] + 127) / 255) << fmt->shift[0]) |
                     (((g * fmt->max[1] + 127) / 255) << fmt->shift[1]) |
                     (((b * fmt->max[2] + 127) / 255) << fmt->shift[2]);
    for (int i = 0; i < fmt->bytes; i++) {
        int shift = (fmt->big_endian ? fmt->bytes - i - 1 : i) * 8;
        *dst++ = value >> shift;
    }
    return dst;
}

// Encode up to 16 rows. Every tile specifies its own colors, so calls can be
// streamed consecutively within one rectangle without carrying tile state.
// Capacity required is width * height * fmt->bytes + ceil(width / 16).
static size_t rfb_hextile(const uint8_t *src, int stride, int width, int height,
                         uint8_t *dst, const rfb_format_t *fmt) {
    uint8_t *start = dst;
    for (int tx = 0; tx < width; tx += 16) {
        int tw = width - tx < 16 ? width - tx : 16;
        const uint8_t *tile = src + tx * 2;
        uint16_t bg = rfb_read565(tile);
        uint16_t fg = bg;
        uint16_t colors[256];
        uint8_t positions[256];
        uint8_t sizes[256];
        int count = 0;
        bool colored = false;
        for (int y = 0; y < height; y++) {
            const uint8_t *row = tile + y * stride;
            int x = 0;
            while (x < tw) {
                uint16_t pixel = rfb_read565(row + x * 2);
                if (pixel == bg) {
                    x++;
                    continue;
                }
                int left = x++;
                while (x < tw && rfb_read565(row + x * 2) == pixel) {
                    x++;
                }
                colors[count] = pixel;
                positions[count] = (left << 4) | y;
                sizes[count] = (x - left - 1) << 4;
                if (count == 0) {
                    fg = pixel;
                } else if (pixel != fg) {
                    colored = true;
                }
                count++;
            }
        }
        size_t raw_size = 1 + tw * height * fmt->bytes;
        size_t encoded_size = 1 + fmt->bytes;
        if (count) {
            encoded_size += 1 + (colored ? 0 : fmt->bytes) +
                            count * (2 + (colored ? fmt->bytes : 0));
        }
        if (encoded_size >= raw_size || count > 255) {
            *dst++ = 1; // Raw
            for (int y = 0; y < height; y++) {
                for (int x = 0; x < tw; x++) {
                    dst = rfb_write_pixel(dst, rfb_read565(tile + y * stride + x * 2), fmt);
                }
            }
        } else {
            *dst++ = 2 | (count ? 8 | (colored ? 16 : 4) : 0);
            dst = rfb_write_pixel(dst, bg, fmt);
            if (count) {
                if (!colored) {
                    dst = rfb_write_pixel(dst, fg, fmt);
                }
                *dst++ = count;
                for (int i = 0; i < count; i++) {
                    if (colored) {
                        dst = rfb_write_pixel(dst, colors[i], fmt);
                    }
                    *dst++ = positions[i];
                    *dst++ = sizes[i];
                }
            }
        }
    }
    return dst - start;
}

#endif
