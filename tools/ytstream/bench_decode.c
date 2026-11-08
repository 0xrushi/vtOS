/* Host-side JPEG correctness probe / CPU benchmark (no display or network).
 * cc -O2 -I modules/st7789/jpg tools/ytstream/bench_decode.c \
 *    modules/st7789/jpg/tjpgd565.c -o /tmp/bench_decode
 * /tmp/bench_decode frame.jpg [scale 0..3] [iterations] > frame.rgb565
 * Prints decode timing to stderr; stdout is one frame in display byte order.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "tjpgd565.h"

typedef struct {
    unsigned char *input, *pixels;
    size_t length, offset, width;
} image_t;

static unsigned int read_jpeg(JDEC *jd, uint8_t *dst, unsigned int n) {
    image_t *image = jd->device;
    if (n > image->length - image->offset) n = image->length - image->offset;
    if (dst) memcpy(dst, image->input + image->offset, n);
    image->offset += n;
    return n;
}

static int write_pixels(JDEC *jd, void *bitmap, JRECT *rect) {
    image_t *image = jd->device;
    size_t row_bytes = 2 * (rect->right - rect->left + 1);
    unsigned char *src = bitmap;
    for (unsigned int y = rect->top; y <= rect->bottom; y++) {
        memcpy(image->pixels + 2 * (y * image->width + rect->left), src, row_bytes);
        src += row_bytes;
    }
    return 1;
}

int main(int argc, char **argv) {
    if (argc < 2 || argc > 4) return 2;
    int scale = argc > 2 ? atoi(argv[2]) : 0;
    int iterations = argc > 3 ? atoi(argv[3]) : 1;
    if (scale < 0 || scale > 3 || iterations < 1) return 2;
    FILE *fp = fopen(argv[1], "rb");
    if (!fp) return 2;
    fseek(fp, 0, SEEK_END);
    long length = ftell(fp);
    rewind(fp);
    if (length <= 0) return 2;
    image_t image = {0};
    image.length = length;
    image.input = malloc(image.length);
    if (!image.input || fread(image.input, 1, image.length, fp) != image.length) return 2;
    fclose(fp);
    unsigned char work[3100];
    JDEC jd;
    int result = jd_prepare(&jd, read_jpeg, work, sizeof(work), &image);
    if (result) return result + 10;
    image.width = jd.width >> scale;
    size_t size = 2 * image.width * (jd.height >> scale);
    image.pixels = calloc(size ? size : 1, 1);
    if (!image.pixels) return 2;
    clock_t start = clock();
    for (int i = 0; i < iterations; i++) {
        image.offset = 0;
        result = jd_prepare(&jd, read_jpeg, work, sizeof(work), &image);
        if (!result) result = jd_decomp(&jd, write_pixels, scale);
        if (result) break;
    }
    fprintf(stderr, "%d iterations: %.3f ms/frame (host CPU only)\n", iterations,
        1000.0 * (clock() - start) / CLOCKS_PER_SEC / iterations);
    if (!result && fwrite(image.pixels, 1, size, stdout) != size) result = 1;
    free(image.pixels);
    free(image.input);
    return result;
}
