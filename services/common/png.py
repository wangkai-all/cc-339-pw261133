"""Minimal PNG reader/writer for grayscale images using only the stdlib.

Supports 8-bit grayscale (color type 0), which is all the fictional
scanner and the extractor need.
"""

import struct
import zlib


def _chunk(tag, data):
    out = struct.pack(">I", len(data)) + tag + data
    return out + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def write_png(path, pixels, width, height):
    """pixels: flat sequence (or list of rows) of gray values 0-255."""
    if pixels and isinstance(pixels[0], (list, tuple)):
        flat = [v for row in pixels for v in row]
    else:
        flat = list(pixels)
    if len(flat) != width * height:
        raise ValueError("pixel buffer size does not match width*height")

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = bytearray()
    stride = width
    for y in range(height):
        raw.append(0)  # filter type 0 (none)
        raw.extend(flat[y * stride:(y + 1) * stride])
    blob = b"\x89PNG\r\n\x1a\n"
    blob += _chunk(b"IHDR", header)
    blob += _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    blob += _chunk(b"IEND", b"")
    with open(path, "wb") as fh:
        fh.write(blob)


def read_png(path):
    """Return (width, height, rows) where rows is a list of byte-lists."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file: %s" % path)

    pos = 8
    width = height = bit_depth = color_type = None
    idat = bytearray()
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, bit_depth, color_type, _, _, _ = struct.unpack(
                ">IIBBBBB", body)
        elif tag == b"IDAT":
            idat.extend(body)
        elif tag == b"IEND":
            break

    if bit_depth != 8 or color_type != 0:
        raise ValueError("only 8-bit grayscale PNGs supported")

    raw = zlib.decompress(bytes(idat))
    stride = width
    rows = []
    p = 0
    prev = bytearray(stride)
    for _ in range(height):
        ftype = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ftype == 1:  # sub
            for i in range(1, stride):
                line[i] = (line[i] + line[i - 1]) & 0xFF
        elif ftype == 2:  # up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:  # average
            for i in range(stride):
                left = line[i - 1] if i else 0
                line[i] = (line[i] + (left + prev[i]) // 2) & 0xFF
        elif ftype == 4:  # paeth
            for i in range(stride):
                a = line[i - 1] if i else 0
                b = prev[i]
                c = prev[i - 1] if i else 0
                est = a + b - c
                pa, pb, pc = abs(est - a), abs(est - b), abs(est - c)
                pred = a if pa <= pb and pa <= pc else (
                    b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        rows.append(line)
        prev = line
    return width, height, rows
