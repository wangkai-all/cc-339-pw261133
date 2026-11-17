"""Generate the fictional scanned pages that ship with the project.

The pages are *rendered* pixel by pixel from the 5x7 bitmap font, put
through a noisy "scan" pass (dropouts, pepper speckles, uneven paper),
and written as grayscale PNGs.  Nothing here is consumed by the
extractor: it only ever sees the PNG pixels.

Usage:
    python3 -I tools/generate_scans.py [out_dir]

Produces:
    <out>/manifest.json
    <out>/receipt-1042/v1/page-001.png ... page-003.png
    <out>/receipt-1042/v2/page-001.png ... page-004.png
"""

import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.common import png as pnglib
from services.common.font5x7 import render_char

WIDTH, HEIGHT = 520, 680
INK = 0
PAPER = 255

# ----------------------------- drawing helpers -----------------------------


def blank_canvas():
    return [[PAPER] * WIDTH for _ in range(HEIGHT)]


def put(img, x, y, val):
    if 0 <= x < WIDTH and 0 <= y < HEIGHT:
        img[y][x] = val


def draw_text(img, text, x, y, scale=3, val=INK):
    """Draw text with one blank base-column between glyphs. Returns end x."""
    cursor = x
    for ch in text.upper():
        w, _, rows = render_char(ch, scale)
        if ch == " ":
            cursor += w + scale
            continue
        for ry, row in enumerate(rows):
            for rx, bit in enumerate(row):
                if bit:
                    for sy in range(1):  # rows already scaled
                        put(img, cursor + rx, y + ry, val)
        cursor += w + scale
    return cursor


def text_width(text, scale=3):
    w = 0
    for ch in text.upper():
        gw, _, _ = render_char(ch, scale)
        w += gw + scale
    return w - scale if text else 0


def draw_hline(img, x0, x1, y, thick=2, val=INK):
    for yy in range(y, y + thick):
        for xx in range(x0, x1):
            put(img, xx, yy, val)


def draw_vline(img, x, y0, y1, thick=2, val=INK):
    for xx in range(x - thick // 2, x - thick // 2 + thick):
        for yy in range(y0, y1):
            put(img, xx, yy, val)


# ------------------------------- page model ---------------------------------

COLUMNS = ["ITEM NO", "QTY", "BIN", "NOTE"]
COL_W = [110, 80, 90, 180]
TABLE_X0 = 30
TABLE_X1 = TABLE_X0 + sum(COL_W)  # 490
HEADER_H = 30
ROW_H = 34

PAGES_V1 = [
    {
        "title": "WAREHOUSE RECEIPT 1042",
        "rows": [
            ["A-101", "12", "B-07", "OK"],
            ["A-102", "8", "B-09", "CHECK"],
            ["C-205", "30", "C-01", "OK"],
            ["D-310", "4", "C-02", "DAMAGED"],
        ],
        "footer": "PAGE 1",
    },
    {
        "title": "WAREHOUSE RECEIPT 1042",
        "rows": [
            ["D-311", "16", "D-04", "OK"],
            ["E-400", "22", "D-05", "LABEL"],
            ["F-502", "9", "E-02", "OK"],
        ],
        "footer": "PAGE 2",
    },
    {
        "title": "WAREHOUSE RECEIPT 1042",
        "rows": [
            ["G-600", "1", "E-08", "FRAGILE"],
            ["H-711", "17", "F-03", "OK"],
        ],
        "footer": "PAGE 3",
    },
]

# v2: an inserted page, one corrected cell, and every table shifted down
# (page-number insertion moved the print frame -> coordinates move while
# the cell content itself stays put).
PAGES_V2 = [
    {
        "title": "WAREHOUSE RECEIPT 1042 REV 2",
        "rows": [
            ["A-101", "12", "B-07", "OK"],
            ["A-102", "8", "B-09", "CHECK"],
            ["C-205", "31", "C-01", "RETRY"],
            ["D-310", "4", "C-02", "DAMAGED"],
        ],
        "footer": "PAGE 1 OF 4",
    },
    {
        "title": "WAREHOUSE RECEIPT 1042 REV 2",
        "rows": [
            ["I-820", "25", "A-01", "NEW"],
            ["I-821", "6", "A-02", "NEW"],
        ],
        "footer": "PAGE 2 OF 4",
    },
    {
        "title": "WAREHOUSE RECEIPT 1042 REV 2",
        "rows": [
            ["D-311", "16", "D-04", "OK"],
            ["E-400", "22", "D-05", "LABEL"],
            ["F-502", "9", "E-02", "OK"],
        ],
        "footer": "PAGE 3 OF 4",
    },
    {
        "title": "WAREHOUSE RECEIPT 1042 REV 2",
        "rows": [
            ["G-600", "1", "E-08", "FRAGILE"],
            ["H-711", "17", "F-03", "OK"],
        ],
        "footer": "PAGE 4 OF 4",
    },
]


def render_page(spec, shift_y=0, seed=1, noise=140, dropout=0.05):
    rng = random.Random(seed)
    img = blank_canvas()

    # uneven paper
    for y in range(HEIGHT):
        for x in range(WIDTH):
            img[y][x] = 255 - rng.randrange(0, 8)

    draw_text(img, spec["title"], TABLE_X0, 26, scale=3)

    n_rows = len(spec["rows"]) + 1  # + header
    table_y0 = 70 + shift_y
    table_y1 = table_y0 + HEADER_H + ROW_H * (n_rows - 1)

    # grid
    xs = [TABLE_X0]
    for w in COL_W:
        xs.append(xs[-1] + w)
    for x in xs:
        draw_vline(img, x, table_y0, table_y1)
    draw_hline(img, TABLE_X0, TABLE_X1, table_y0)
    draw_hline(img, TABLE_X0, TABLE_X1, table_y0 + HEADER_H)
    for r in range(len(spec["rows"])):
        draw_hline(img, TABLE_X0, TABLE_X1,
                   table_y0 + HEADER_H + (r + 1) * ROW_H)

    # header text
    for ci, col in enumerate(COLUMNS):
        scale = 3 if text_width(col, 3) < COL_W[ci] - 12 else 2
        draw_text(img, col, xs[ci] + 6, table_y0 + 7, scale=scale)

    # data text
    for ri, row in enumerate(spec["rows"]):
        for ci, val in enumerate(row):
            scale = 3 if text_width(val, 3) < COL_W[ci] - 12 else 2
            draw_text(img, val, xs[ci] + 6,
                      table_y0 + HEADER_H + ri * ROW_H + 8, scale=scale)

    draw_text(img, spec["footer"], TABLE_X1 - text_width(spec["footer"], 2),
              HEIGHT - 40, scale=2)

    # ---- scan degradation ----
    # ink dropouts: lose a small fraction of dark glyph pixels
    for y in range(HEIGHT):
        for x in range(WIDTH):
            if img[y][x] < 128 and rng.random() < dropout:
                img[y][x] = rng.randrange(190, 235)
    # pepper speckles
    for _ in range(noise):
        x, y = rng.randrange(WIDTH), rng.randrange(HEIGHT)
        img[y][x] = rng.randrange(20, 90)
        if rng.random() < 0.3:  # occasional 2px fleck
            put(img, x + 1, y, rng.randrange(20, 90))

    return img


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else \
        os.path.join(os.getcwd(), "sample_input")
    doc = "receipt-1042"
    manifest = {"document": doc, "versions": {}}

    for version, pages, shift in (("v1", PAGES_V1, 0),
                                  ("v2", PAGES_V2, 14)):
        vdir = os.path.join(out, doc, version)
        os.makedirs(vdir, exist_ok=True)
        files = []
        for i, spec in enumerate(pages):
            seed = 1000 + hash_version(version) + i * 17
            img = render_page(spec, shift_y=shift, seed=seed)
            fname = "page-%03d.png" % (i + 1)
            pnglib.write_png(os.path.join(vdir, fname), img, WIDTH, HEIGHT)
            files.append(fname)
        manifest["versions"][version] = {"files": files}

    with open(os.path.join(out, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    print("wrote fictional scans to", out)


def hash_version(v):
    return 11 if v == "v1" else 29


if __name__ == "__main__":
    main()
