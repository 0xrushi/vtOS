"""Native decoder pixel regressions, including the actual STRIP display callback."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures/ytjpeg"


def compile_probe(source, output):
    compiler = shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler not installed")
    flags = []
    if sys.platform == "darwin":
        # Prefer the linker paired with the active Apple SDK/toolchain.
        linker = subprocess.check_output(["xcrun", "--find", "ld"], text=True).strip()
        sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
        flags = ["--ld-path=" + linker, "-isysroot", sdk]
    subprocess.run([compiler, *flags, "-O2", "-Wall", "-Wextra", "-I", str(ROOT / "modules/st7789/jpg"),
                    str(source), str(ROOT / "modules/st7789/jpg/tjpgd565.c"), "-o", str(output)], check=True)
    return output


@pytest.fixture(scope="module")
def decoder(tmp_path_factory):
    output = tmp_path_factory.mktemp("jpeg") / "decode"
    return compile_probe(ROOT / "tools/ytstream/bench_decode.c", output)


@pytest.mark.parametrize("sampling", ["420", "422", "444"])
@pytest.mark.parametrize("scale", range(4))
def test_native_decoder_matches_original_pixels(decoder, sampling, scale):
    # These hashes were captured from the original decoder before optimizing it.
    expected = json.loads((FIXTURES / "expected.json").read_text())[sampling + ":" + str(scale)]
    data = subprocess.check_output([str(decoder), str(FIXTURES / (sampling + ".jpg")), str(scale)])
    assert len(data) == expected["bytes"]
    assert hashlib.sha256(data).hexdigest() == expected["sha256"]


def test_native_decoder_rejects_truncated_frame(decoder, tmp_path):
    path = tmp_path / "torn.jpg"
    data = (FIXTURES / "420.jpg").read_bytes()
    path.write_bytes(data[:len(data) // 2])
    assert subprocess.run([str(decoder), str(path)], capture_output=True).returncode != 0


@pytest.fixture(scope="module")
def strip_decoder(tmp_path_factory):
    folder = tmp_path_factory.mktemp("jpeg-strip")
    driver = (ROOT / "modules/st7789/st7789.c").read_text()
    start = driver.index("static void copy_strip(")
    end = driver.index("\n}\n", driver.index("static int out_fill(", start)) + 3
    # Compile the production callback against a framebuffer that emulates SPI.
    # Offset placement, clipping of the last MCU, and transaction count are real.
    source = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "tjpgd565.h"
typedef struct {
    unsigned char *pixels;
    unsigned int width, height, x0, y0, x1, y1, transfers;
    void *spi_obj;
} st7789_ST7789_obj_t;
typedef struct {
    unsigned char *fbuf, *data;
    unsigned int wfbuf, offset, length;
    st7789_ST7789_obj_t *self;
} IODEV;
#define DC_HIGH() ((void)0)
#define CS_LOW() ((void)0)
#define CS_HIGH() ((void)0)
#define JPG_FILL_ROWS 8
#define MIN(a,b) ((a)<(b) ? (a) : (b))
static void set_window(st7789_ST7789_obj_t *self, int x0, int y0, int x1, int y1) {
    if (x0<0 || y0<0 || x1>=(int)self->width || y1>=(int)self->height) abort();
    self->x0=x0; self->y0=y0; self->x1=x1; self->y1=y1;
}
static void write_spi(void *obj, const uint8_t *data, int size) {
    st7789_ST7789_obj_t *self = obj;
    unsigned int stride = 2 * (self->x1-self->x0+1);
    if ((unsigned int)size != stride * (self->y1-self->y0+1)) abort();
    for (unsigned int y=self->y0; y<=self->y1; y++) {
        memcpy(self->pixels + 2*(y*self->width+self->x0), data, stride);
        data += stride;
    }
    self->transfers++;
}
CALLBACK
static unsigned int input(JDEC *jd, uint8_t *dst, unsigned int n) {
    IODEV *dev=jd->device;
    if (n > dev->length-dev->offset) n=dev->length-dev->offset;
    if (dst) memcpy(dst, dev->data+dev->offset, n);
    dev->offset += n;
    return n;
}
int main(int argc, char **argv) {
    if (argc != 2 && argc != 4) return 2;
    int fill=argc==4;
    FILE *fp=fopen(argv[1], "rb");
    if (!fp) return 2;
    IODEV dev={0};
    unsigned char data[65536], work[3100];
    dev.data=data; dev.length=fread(data, 1, sizeof(data), fp); fclose(fp);
    JDEC jd;
    if (jd_prepare(&jd, input, work, sizeof(work), &dev)) return 3;
    st7789_ST7789_obj_t display={0};
    display.width=fill ? atoi(argv[2]) : jd.width+6;
    display.height=fill ? atoi(argv[3]) : jd.height+10;
    display.spi_obj=&display;
    size_t size=2*display.width*display.height;
    display.pixels=malloc(size); memset(display.pixels, 0xA5, size);
    dev.self=&display; dev.wfbuf=jd.width;
    dev.fbuf=malloc(2*(jd.width*jd.msy*8+(fill ? JPG_FILL_ROWS*display.width : 0)));
    jd.x_offs=3; jd.y_offs=5;
    if (jd_decomp(&jd, fill ? out_fill : out_strip, 0)) return 4;
    if (!fill && display.transfers != (jd.height+jd.msy*8-1)/(jd.msy*8)) return 5;
    fwrite(display.pixels, 1, size, stdout);
    free(dev.fbuf); free(display.pixels);
    return 0;
}
'''.replace("CALLBACK", driver[start:end])
    path = folder / "strip.c"
    path.write_text(source)
    return compile_probe(path, folder / "strip")


@pytest.mark.parametrize("sampling,width,height", [("420", 32, 32), ("422", 35, 19), ("444", 19, 17)])
def test_strip_matches_full_frame_and_preserves_border(decoder, strip_decoder, sampling, width, height):
    path = FIXTURES / (sampling + ".jpg")
    full = subprocess.check_output([str(decoder), str(path)])
    strip = subprocess.check_output([str(strip_decoder), str(path)])
    expected = bytearray(b"\xa5" * (2 * (width + 6) * (height + 10)))
    for y in range(height):
        start = 2 * ((y + 5) * (width + 6) + 3)
        expected[start:start + 2 * width] = full[2 * y * width:2 * (y + 1) * width]
    assert strip == expected


@pytest.mark.parametrize("sampling,width,height", [("420", 32, 32), ("422", 35, 19), ("444", 19, 17)])
def test_fill_scales_every_display_pixel(decoder, strip_decoder, sampling, width, height):
    path = FIXTURES / (sampling + ".jpg")
    full = subprocess.check_output([str(decoder), str(path)])
    scaled = subprocess.check_output([str(strip_decoder), str(path), "320", "240"])
    expected = bytearray()
    for y in range(240):
        for x in range(320):
            src = 2 * ((y * height // 240) * width + x * width // 320)
            expected.extend(full[src:src + 2])
    assert scaled == expected
