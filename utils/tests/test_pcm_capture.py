"""Compile and exercise the actual I2S PCM frame packer on fragmented input."""

import ctypes
from pathlib import Path
import shutil
import struct
import subprocess

import pytest


@pytest.fixture(scope="module")
def packer(tmp_path_factory):
    compiler = shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler unavailable")
    directory = tmp_path_factory.mktemp("pcm-capture")
    source = directory / "wrapper.c"
    source.write_text(
        '#include "pcm_capture.h"\n'
        'size_t pack(uint8_t *b, size_t n, uint8_t *c, size_t *l, bool mono) {\n'
        '  return vtos_capture_pack(b, n, c, l, mono);\n}\n'
    )
    include = Path(__file__).resolve().parents[2] / "modules/tdeck_i2s"
    library = directory / "pcm.so"
    subprocess.run([compiler, "-shared", "-fPIC", "-std=c99", "-Wall", "-Werror",
                    "-I", str(include), str(source), "-o", str(library)], check=True)
    function = ctypes.CDLL(str(library)).pack
    pointer = ctypes.POINTER(ctypes.c_uint8)
    function.argtypes = [pointer, ctypes.c_size_t, pointer,
                         ctypes.POINTER(ctypes.c_size_t), ctypes.c_bool]
    function.restype = ctypes.c_size_t
    return function


@pytest.mark.parametrize("mono", [False, True])
@pytest.mark.parametrize("fragment_size", [1, 2, 3, 5, 7, 11, 1023])
def test_fragmented_stereo_frames_are_preserved(packer, mono, fragment_size):
    frames = [(-32768, 32767), (1234, -2345), (0, 0), (31000, -31000)] * 20
    pcm = b"".join(struct.pack("<hh", *frame) for frame in frames)
    # An incomplete final frame must remain in carry, never corrupt sample 0.
    pcm += b"\xaa\xbb\xcc"
    carry = (ctypes.c_uint8 * 4)()
    length = ctypes.c_size_t(0)
    result = bytearray()
    for offset in range(0, len(pcm), fragment_size):
        part = pcm[offset:offset + fragment_size]
        data = bytes(carry[:length.value]) + part
        chunk = (ctypes.c_uint8 * (len(data) + 4))()
        chunk[:len(data)] = data
        count = packer(chunk, len(data), carry, ctypes.byref(length), mono)
        result.extend(chunk[:count])
    expected = (b"".join(struct.pack("<h", left) for left, _ in frames)
                if mono else pcm[:-3])
    assert result == expected
    assert bytes(carry[:length.value]) == pcm[-3:]
