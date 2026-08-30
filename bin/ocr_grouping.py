"""OCR line -> paragraph/block grouping - pure Python (bbox/text/conf dicts
and tuples only), no image-library dependency at all.

Split out of ocr_worker.py (2026-08-30): windows_ocr_engine.py imports
group_lines_into_blocks() from here too (Windows.Media.Ocr has the same
flat-lines-only shape problem Tesseract's SPARSE_TEXT does - see
group_lines_into_blocks()'s own docstring), but ocr_worker.py imports PIL
unconditionally at module level for its own Tesseract-specific code -
confirmed live 2026-08-29 that pulling group_lines_into_blocks() from
ocr_worker.py directly meant a clean Windows install (no Pillow in
requirements.txt, and nothing else in the Windows port needs it) hit
ModuleNotFoundError: PIL the moment WindowsOcrEngine tried to import it,
silently leaving the OCR engine unconfigured. This module has zero
dependencies beyond the stdlib so it can be shared without dragging in
anything Windows-only code doesn't actually need.
"""
import statistics


def _bbox_area(bbox):
    x0, y0, x1, y1 = bbox
    return max(0, x1 - x0) * max(0, y1 - y0)


def _overlap_area(a, b):
    ox = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    oy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return ox * oy


def _drop_contained_groups(groups, containment_ratio=0.6):
    """Drop groups that are mostly nested inside a larger group's bbox.

    Confirmed live against a real NORCO frame: Tesseract's line detector
    can emit small duplicate fragments ("vast", "saw an") sitting entirely
    inside a large paragraph's bbox that already contains that same text -
    verified by re-running OCR on the identical static image and getting
    byte-identical output both times, ruling out engine non-determinism.
    These nested duplicates were the main source of the block set
    thrashing on that scene: as isolated word-fragments they carry no
    context, translate to noise, and constantly re-trigger the flaky-block
    guard. Kept the larger paragraph group (which read correctly and
    consistently) and drop whatever's mostly inside it, rather than trying
    to fix Tesseract's segmentation upstream.
    """
    ordered = sorted(groups, key=lambda g: -_bbox_area(g["bbox"]))
    kept = []
    for g in ordered:
        area = _bbox_area(g["bbox"])
        if area <= 0:
            continue
        if any(_overlap_area(g["bbox"], k["bbox"]) / area >= containment_ratio for k in kept):
            continue
        kept.append(g)
    return kept


def _merge_same_row_fragments(lines, horizontal_gap_ratio=1.5):
    """Merge OCR "lines" that actually sit on the same visual row into one.

    Confirmed live against a real game frame: Tesseract's SPARSE_TEXT line
    segmentation sometimes splits a single visual line into multiple
    same-row RIL.TEXTLINE fragments wherever the gap between words is
    unusually wide (e.g. "You spoke to Blake," and "learning" - one
    sentence, one row - came back as two separate "lines" 18px apart
    horizontally). Left unmerged, group_lines_into_blocks()'s vertical/
    horizontal-overlap paragraph merge below has no way to recombine them
    (they don't overlap in x, which is exactly the signal it uses to
    distinguish a wrapped paragraph line from unrelated text) - so a
    fragment like "learning" ends up either as its own disconnected
    single-word block, or silently deleted by _drop_contained_groups() for
    incidentally overlapping a neighboring (also wrongly split) paragraph
    block. Merging same-row fragments first, before that pass runs, fixes
    both: the words end up back in the right sentence, and there's nothing
    left over for _drop_contained_groups() to eat.
    """
    ordered = sorted(lines, key=lambda line: (line["bbox"][1], line["bbox"][0]))
    merged = []
    for line in ordered:
        x0, y0, x1, y1 = line["bbox"]
        for existing in merged:
            ex0, ey0, ex1, ey1 = existing["bbox"]
            vertical_overlap = min(y1, ey1) - max(y0, ey0)
            same_row = vertical_overlap > 0 and vertical_overlap >= 0.5 * min(y1 - y0, ey1 - ey0)
            gap_x = max(x0 - ex1, ex0 - x1, 0)
            # Sort order guarantees x0 ascending within a row, so a match
            # is always further right than what's already merged - safe to
            # always append rather than figure out left/right insertion.
            close_enough = gap_x <= horizontal_gap_ratio * max(y1 - y0, ey1 - ey0)
            if same_row and close_enough:
                existing["bbox"] = (min(x0, ex0), min(y0, ey0), max(x1, ex1), max(y1, ey1))
                existing["text"] = existing["text"] + " " + line["text"]
                existing["conf"] = min(existing["conf"], line["conf"])
                break
        else:
            merged.append({"bbox": (x0, y0, x1, y1), "text": line["text"], "conf": line["conf"]})
    return merged


def group_lines_into_blocks(lines, vertical_gap_ratio=0.8, horizontal_overlap_ratio=0.3):
    """Merge individual OCR text lines into paragraph-like blocks.

    Tesseract's PSM.SPARSE_TEXT doesn't build real paragraph/block structure -
    verified empirically against real game frames: iterating at RIL.PARA or
    RIL.BLOCK returns exactly the same one-line-per-item output as
    RIL.TEXTLINE, since sparse mode assumes scattered, layout-less text. A
    dialogue message that wraps across N lines therefore comes back as N
    separate same-priority "blocks" unless grouped here - which then get
    discovered/tracked/translated as unrelated fragments instead of one
    coherent message.

    Two lines merge when they're vertically close *relative to their own
    line height* (not a fixed pixel constant, so this scales across font
    sizes/DPI/game resolutions) and their horizontal spans substantially
    overlap - the overlap check is what distinguishes a wrapped paragraph
    from an unrelated line that happens to sit at a similar height (e.g. a
    HUD icon off to the side of a dialogue box).

    vertical_gap_ratio defaults to 0.8, not a tighter-looking 0.6 - measured
    live against a real game frame's line pitch (row-to-row gaps of 6-10px
    against line heights of 12-17px put the *real* ratio right around
    0.6-0.7 with essentially no margin, so normal OCR bbox jitter of a
    couple px was enough to miss the tolerance check and split a paragraph
    mid-sentence). Inter-paragraph gaps in the same frame were 30px+, so
    0.8 still leaves a wide margin before that - checked up to 1.0 without
    any false merges across real paragraph boundaries.

    Input: [{"text", "conf", "bbox": (x0,y0,x1,y1)}, ...]
    Output: [{"id", "text", "conf", "bbox": {"x0","y0","x1","y1"}}, ...]
    """
    ordered = _merge_same_row_fragments(lines)
    groups = []
    for line in ordered:
        x0, y0, x1, y1 = line["bbox"]
        for group in groups:
            gx0, gy0, gx1, gy1 = group["bbox"]
            line_h = y1 - y0
            # Median of the *individual* line heights merged so far, not the
            # group bbox's total span (gy1 - gy0) - the latter grows with
            # every absorbed line, so the gap tolerance below would keep
            # widening and make a group progressively more eager to pull in
            # unrelated lines underneath it. Median (vs. e.g. the previous
            # line alone) also survives one stray mis-sized line without
            # throwing off tolerance for the rest of the group.
            ref_h = statistics.median(group["heights"])
            gap = max(y0 - gy1, 0)
            vertical_ok = gap <= vertical_gap_ratio * max(line_h, ref_h, 1)
            overlap = min(x1, gx1) - max(x0, gx0)
            shorter_width = min(x1 - x0, gx1 - gx0)
            horizontal_ok = shorter_width > 0 and overlap / shorter_width >= horizontal_overlap_ratio
            if vertical_ok and horizontal_ok:
                group["bbox"] = (min(x0, gx0), min(y0, gy0), max(x1, gx1), max(y1, gy1))
                group["texts"].append(line["text"])
                group["confs"].append(line["conf"])
                group["heights"].append(line_h)
                break
        else:
            groups.append(
                {"bbox": (x0, y0, x1, y1), "texts": [line["text"]], "confs": [line["conf"]], "heights": [y1 - y0]}
            )

    groups = _drop_contained_groups(groups)

    blocks = []
    for group_id, group in enumerate(groups):
        gx0, gy0, gx1, gy1 = group["bbox"]
        blocks.append(
            {
                "id": group_id,
                "text": " ".join(group["texts"]),
                "conf": round(min(group["confs"]), 1),
                "bbox": {"x0": gx0, "y0": gy0, "x1": gx1, "y1": gy1},
            }
        )
    return blocks
