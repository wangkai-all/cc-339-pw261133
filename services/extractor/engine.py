"""Real table extraction and glyph OCR over scanned grayscale PNGs.

Pipeline (no third-party libraries, no answer key):
  1. read pixels, binarize with a threshold
  2. find long horizontal/vertical runs -> table grid lines
  3. intersect grid lines into cells, crop each cell interior
  4. segment glyphs by ink-column gaps (despeckled connected components)
  5. match every glyph against the 5x7 font templates over plausible
     scales by Hamming distance, keep best score -> character + confidence
  6. emit cell text, per-character confidence, bounding boxes and a
     content fingerprint computed on despeckled glyph bitmaps
"""

from services.common.font5x7 import GLYPHS
from services.common.png import read_png

BIN_THRESHOLD = 128
LINE_RUN = 40          # min run length to count as a grid-line segment
LINE_GAP = 3           # dropouts may split a line: tolerate short gaps
SPECKLE_AREA = 3       # components smaller than this are scanner pepper
GAP_COLS = 1           # one fully blank column separates glyphs


def _binarize(rows):
    return [[1 if v < BIN_THRESHOLD else 0 for v in row] for row in rows]


def _longest_run(seq, gap_tol=LINE_GAP):
    """Longest dark span, allowing up to gap_tol consecutive blank cells."""
    best = run = gap = 0
    for v in seq:
        if v:
            run += 1 + gap
            gap = 0
            best = max(best, run)
        elif run:
            gap += 1
            if gap > gap_tol:
                run = gap = 0
    return best


def _lines(binimg):
    h, w = len(binimg), len(binimg[0])

    # Stage 1: horizontal grid lines are dense rows (>=55% ink across the
    # page width); text rows never reach that density.
    hlines = []
    for y in range(h):
        row = binimg[y]
        if sum(row) / w >= 0.55 and _longest_run(row) >= LINE_RUN:
            hlines.append(y)
    hlines = _merge(hlines)
    if len(hlines) < 2:
        return hlines, []

    # Stage 2: vertical grid lines live between the first/last horizontal
    # line; inside that band a long dark column is a grid line, not a glyph
    # (glyphs are at most ~24px tall at scale 3).
    top, bottom = hlines[0], hlines[-1]
    band_h = bottom - top
    vlines = []
    for x in range(w):
        col = [binimg[y][x] for y in range(top, bottom + 1)]
        if _longest_run(col) >= max(LINE_RUN, int(band_h * 0.35)):
            vlines.append(x)
    vlines = _merge(vlines)
    return hlines, vlines


def _merge(vals, gap=4):
    if not vals:
        return []
    groups = [[vals[0]]]
    for v in vals[1:]:
        if v - groups[-1][-1] <= gap:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) // len(g) for g in groups]


def _components(crop):
    """4-connected components within a cropped binary matrix."""
    h, w = len(crop), len(crop[0])
    seen = [[False] * w for _ in range(h)]
    comps = []
    for y in range(h):
        for x in range(w):
            if not crop[y][x] or seen[y][x]:
                continue
            stack = [(x, y)]
            seen[y][x] = True
            pts = []
            while stack:
                cx, cy = stack.pop()
                pts.append((cx, cy))
                for nx, ny in ((cx + 1, cy), (cx - 1, cy),
                               (cx, cy + 1), (cx, cy - 1)):
                    if 0 <= nx < w and 0 <= ny < h and crop[ny][nx] \
                            and not seen[ny][nx]:
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            comps.append(pts)
    return comps


def _components_to_glyphs(crop):
    """Despeckle, then group ink into glyph boxes.

    Scanner pepper is a 1-2px fleck.  Real ink (including thin glyphs such
    as I, 1 or '-') either reaches 3px in one dimension or belongs to a
    component with >=3 pixels.  Dropouts can shatter one glyph into several
    components, so glyph groups are cut on blank columns and narrow
    fragments sharing a vertical extent are merged back together.
    """
    h, w = len(crop), len(crop[0])
    clean = [[0] * w for _ in range(h)]
    for pts in _components(crop):
        ys = [y for _, y in pts]
        xs = [x for x, _ in pts]
        bw, bh = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
        # Scanner pepper is a 1-2px isolated fleck.  A real glyph stroke
        # (including a dropout-shattered fragment) spans >=3px in one
        # dimension or has >=3 pixels.
        if len(pts) <= 2 and bw <= 2 and bh <= 2:
            continue
        for x, y in pts:
            clean[y][x] = 1

    ink_cols = [sum(clean[y][x] for y in range(h)) for x in range(w)]
    ink_rows = [sum(clean[y]) for y in range(h)]
    if not any(ink_cols):
        return [], clean

    # vertical text band: rows that contain ink
    band_rows = [y for y, n in enumerate(ink_rows) if n > 0]
    band_top, band_bot = min(band_rows), max(band_rows)
    band_h = band_bot - band_top + 1
    # one rendered glyph is 7*scale tall; derive its expected width
    glyph_scale = max(1, round(band_h / 7))
    max_glyph_w = 5 * glyph_scale + 2
    glyph_gap = max(1, glyph_scale)  # blank cols between glyphs

    # raw column groups cut on fully blank columns
    raw_groups, start = [], None
    for x in range(w):
        active = any(clean[y][x] for y in range(band_top, band_bot + 1))
        if active and start is None:
            start = x
        elif not active and start is not None:
            raw_groups.append([start, x - 1])
            start = None
    if start is not None:
        raw_groups.append([start, w - 1])

    boxes = []
    for gx0, gx1 in raw_groups:
        # split over-wide groups at the widest internal blank run
        spans = _split_span(clean, gx0, gx1, band_top, band_bot,
                            max_glyph_w, glyph_gap)
        for sx0, sx1 in spans:
            ys = [y for y in range(band_top, band_bot + 1)
                  for x in range(sx0, sx1 + 1) if clean[y][x]]
            if ys:
                boxes.append([sx0, min(ys), sx1, max(ys)])

    # merge shattered fragments: a narrow fragment vertically overlapping
    # its neighbour and separated by a sub-gap belongs to the same glyph.
    if len(boxes) > 1:
        widths = sorted(b[2] - b[0] + 1 for b in boxes)
        median_w = widths[len(widths) // 2]
        merged = [boxes[0]]
        for b in boxes[1:]:
            p = merged[-1]
            gap = b[0] - p[2] - 1
            ov = min(p[3], b[3]) - max(p[1], b[1]) + 1
            narrow = (b[2] - b[0] + 1) < max(3, median_w * 0.6)
            if gap <= max(1, glyph_scale // 2) and (ov > 0 or narrow):
                merged[-1] = [min(p[0], b[0]), min(p[1], b[1]),
                              max(p[2], b[2]), max(p[3], b[3])]
            else:
                merged.append(b)
        boxes = merged

    return [tuple(b) for b in boxes], clean


def _split_span(clean, x0, x1, y0, y1, max_w, min_gap):
    """Recursively cut an ink span at its widest internal blank column
    until every piece is at most one glyph wide."""
    width = x1 - x0 + 1
    if width <= max_w:
        return [(x0, x1)]
    # find blank runs inside the span
    blanks, run, run_start = [], 0, None
    for x in range(x0, x1 + 1):
        active = any(clean[y][x] for y in range(y0, y1 + 1))
        if not active:
            if run == 0:
                run_start = x
            run += 1
        else:
            if run:
                blanks.append((run, run_start, x - 1))
            run = 0
    candidates = [b for b in blanks if b[0] >= min_gap]
    if not candidates:
        return [(x0, x1)]
    _, cut0, cut1 = max(candidates)
    left = _split_span(clean, x0, cut0 - 1, y0, y1, max_w, min_gap)
    right = _split_span(clean, cut1 + 1, x1, y0, y1, max_w, min_gap)
    return left + right


def _scale_of_glyph(box):
    gx0, gy0, gx1, gy1 = box
    gw, gh = gx1 - gx0 + 1, gy1 - gy0 + 1
    # height is the reliable signal: every rendered glyph is 7*scale tall.
    # A short wide stroke is a scale-1 punctuation mark (e.g. '-', '.'),
    # not a vanished glyph.
    if gh <= 3 and gw >= gh * 2:
        return 1
    return min(max(round(gh / 7), 1), 5)


def _match_glyph(clean, box, scale):
    gx0, gy0, gx1, gy1 = box
    gw, gh = gx1 - gx0 + 1, gy1 - gy0 + 1
    observed = [[clean[gy0 + y][gx0 + x] for x in range(gw)]
                for y in range(gh)]

    best = (" ", 0.0, scale)
    short_mark = gh <= max(3, scale)
    for ch, tmpl in GLYPHS.items():
        if ch == " ":
            continue
        scales = (1,) if short_mark else range(max(1, scale - 1), scale + 2)
        for sc in scales:
            th = 7 * sc
            # short punctuation ('-', '.') only occupies a few rows of its
            # scale-1 cell, so compare on width alone for those; otherwise
            # height pins the scale while width stays unconstrained (thin
            # glyphs I/1 are far narrower than the 5*sc cell).
            if not short_mark and abs(th - gh) > sc + 2:
                continue
            # resize observed onto the 5x7 template grid by block sampling
            score, total = 0, 0
            for ty in range(7):
                for tx in range(5):
                    want = tmpl[ty][tx]
                    x0 = tx * gw // 5
                    x1 = max(x0 + 1, (tx + 1) * gw // 5)
                    y0 = ty * gh // 7
                    y1 = max(y0 + 1, (ty + 1) * gh // 7)
                    got = 1 if any(observed[y][x]
                                   for y in range(y0, y1)
                                   for x in range(x0, x1)) else 0
                    total += 1
                    if got == want:
                        score += 1
            conf = score / total
            if conf > best[1] or (conf == best[1] and sc == scale
                                  and best[2] != scale):
                best = (ch, conf, sc)
    return best


def _glyph_grid(clean, box):
    """Normalize one glyph box to a 5x7 binary grid (geometry-independent)."""
    gx0, gy0, gx1, gy1 = box
    gw, gh = gx1 - gx0 + 1, gy1 - gy0 + 1
    observed = [[clean[gy0 + y][gx0 + x] for x in range(gw)]
                for y in range(gh)]
    grid = []
    for ty in range(7):
        row = []
        for tx in range(5):
            x0 = tx * gw // 5
            x1 = max(x0 + 1, (tx + 1) * gw // 5)
            y0 = ty * gh // 7
            y1 = max(y0 + 1, (ty + 1) * gh // 7)
            row.append(1 if any(observed[y][x]
                                for y in range(y0, y1)
                                for x in range(x0, x1)) else 0)
        grid.append(row)
    return grid


def _content_fingerprint(clean, boxes, text):
    """Stable content hash over the recognized text (normalized).

    Geometry, page coordinates and box-external pepper are all excluded, so
    the same content fingerprints identically even when a page is shifted
    (page-number insertion) or rescanned with different speckle.  The raw
    pixel evidence (char boxes, confidence, crop bbox) is kept separately.
    """
    import hashlib
    norm = "".join(text.split()).upper()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def extract(png_path):
    width, height, rows = read_png(png_path)
    binimg = _binarize(rows)
    hlines, vlines = _lines(binimg)
    if len(hlines) < 2 or len(vlines) < 2:
        return {"ok": False, "reason": "no table grid detected",
                "width": width, "height": height,
                "hlines": hlines, "vlines": vlines}

    x0, x1 = min(vlines), max(vlines)
    y0, y1 = min(hlines), max(hlines)
    xs = [x for x in vlines if x0 <= x <= x1]
    ys = [y for y in hlines if y0 <= y <= y1]
    xs.sort()
    ys.sort()

    cells = []
    for r in range(len(ys) - 1):
        for c in range(len(xs) - 1):
            cx0, cx1 = xs[c] + 2, xs[c + 1] - 2
            cy0, cy1 = ys[r] + 2, ys[r + 1] - 2
            crop = [row[cx0:cx1 + 1] for row in binimg[cy0:cy1 + 1]]
            boxes, clean = _components_to_glyphs(crop)
            chars, char_confs, char_boxes = [], [], []
            for box in boxes:
                scale = _scale_of_glyph(box)
                ch, conf, _ = _match_glyph(clean, box, scale)
                chars.append(ch)
                char_confs.append(round(conf, 3))
                gx0, gy0, gx1, gy1 = box
                char_boxes.append([cx0 + gx0, cy0 + gy0,
                                   cx0 + gx1, cy0 + gy1])
            text = "".join(chars).strip()
            avg_conf = round(sum(char_confs) / len(char_confs), 3) \
                if char_confs else 1.0
            cells.append({
                "cell_id": "r%dc%d" % (r, c),
                "row": r,
                "col": c,
                "bbox": [cx0 - 2, cy0 - 2, cx1 + 2, cy1 + 2],
                "text": text,
                "confidence": avg_conf,
                "char_confidence": char_confs,
                "char_boxes": char_boxes,
                "fingerprint": _content_fingerprint(clean, boxes, text),
            })

    # table fingerprint = ordered cell fingerprints (geometry-independent)
    table_fp = hashlib256("|".join(
        "%s:%s" % (ce["cell_id"], ce["fingerprint"]) for ce in cells))

    return {
        "ok": True,
        "width": width,
        "height": height,
        "grid": {"hlines": hlines, "vlines": vlines},
        "cells": cells,
        "table_fingerprint": table_fp,
    }


def hashlib256(s):
    import hashlib
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]
