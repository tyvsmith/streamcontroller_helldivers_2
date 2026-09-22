"""Calibrated recognition geometry shared by the icon matchers.

Every value here changes matching results, so this file is part of the
recognition cache fingerprint (``recognition_cache.scanner_cache``). Changing a
value is a recalibration, not a refactor: rerun the full-scene fixtures.

Frame conventions
-----------------
Matchers measure a tile after ignoring its square frame, or read the frame
itself. Each convention was tuned on its own, and they differ in basis, minimum
and purpose. Equal values are coincidence, not a shared rule; do not merge them.

Ignore the frame (trim before measuring):

    name                      value            basis                   use
    NORMALIZE_RIM_*           .07 of side, >=2  min(tile w, h)          normalize_icon foreground
    COLORLESS_RIM_*           .07 of side, >=1  min(tile h, w)          glyph_mask on captures
    TEMPLATE_INTERIOR         [23:121]         catalog icon px          template glyph interior
    STRETCH_RIM_*             .10 of side, >=2  min(tile w, h)          stretch_icon floor sample
    OCCLUSION_INTERIOR        .12/.12/.88/.60  tile w, h (l, t, r, b)  icon_occluded white test
    EQUIPPED_FEATURE_INSET    8 px             matched tile px          match_equipped features
    EQUIPPED_DETAIL_INSET     12 px            matched tile px          match_equipped components
    ICON_GRAY_INSET           6 px             matched tile px          detect_icons gray/edges
    ICON_SILHOUETTE_INSET     12 px            matched tile px          detect_icons silhouette
    MISSION_REFERENCE_INSET   8 px             MISSION_REFERENCE_PX     reference detail and badge

Read the frame (the band is the evidence):

    OCCUPANCY_RIM_*           .096 of side, >=2  min(box w, h)        mission slot frame occupancy
    MISSION_FRAME_BORDER_*    .057 of side, >=2  HUD icon size        mission_layout border stroke

Left as literals: the equipped empty-tile test samples the centre half with
``w // 4`` and ``3 * w // 4``, and the frame hue strip in ``icon_normalization``
(rows .2-.8, columns .01-.05) samples the frame's left stroke rather than
trimming it.
"""

# Side length that equipped and normalized-retry matching resizes a tile to.
# Mission tiles and normalized retries are resized to it; selection crops are
# scaled so Ready-bar equipped tiles arrive at about this size.
TILE_PX = 150

# Glyph interior of a catalog icon (``assets/icons``), cut before templates,
# features and colorless masks are built. On the 144-pixel icons it drops a
# 23-pixel frame on each side.
TEMPLATE_INTERIOR = slice(23, 121)

# Square canvas that ``silhouette`` centres a thresholded glyph mask on, and
# the longest side the mask is scaled to inside it (a 4-pixel margin).
SILHOUETTE_CANVAS = 64
SILHOUETTE_EXTENT = 56

# Square grid that arrow-sequence glyphs and their templates are rasterized to.
# The template polygon's vertices in ``mission_fallbacks`` are drawn in it.
ARROW_GLYPH = 40

# Side length of the curated game reference crops (``scanner/references``);
# captured mission tiles are resized to it before comparison.
MISSION_REFERENCE_PX = 87

# normalize_icon: the colored frame is excluded from the foreground so its hue
# and white strokes cannot be taken as glyph pixels.
NORMALIZE_RIM_FRACTION = .07
NORMALIZE_RIM_MIN = 2

# glyph_mask on a captured tile: the frame is cut before per-row background and
# foreground percentiles, so a frame stroke cannot set a row's contrast. The
# template branch uses TEMPLATE_INTERIOR instead. Note the smaller minimum.
COLORLESS_RIM_FRACTION = .07
COLORLESS_RIM_MIN = 1

# stretch_icon: the per-channel floor (5th percentile) is sampled inside this
# rim so the frame and any darker surround do not pull the floor down.
STRETCH_RIM_FRACTION = .10
STRETCH_RIM_MIN = 2

# icon_occluded: (left, top, right, bottom) fractions of the region tested for a
# solid white overlay. It skips the frame and the lower 40 percent of the tile.
OCCLUSION_INTERIOR = (.12, .12, .88, .60)

# match_equipped: pixels trimmed from each side of the matched tile before the
# feature correlation, and the deeper trim before the component silhouettes.
EQUIPPED_FEATURE_INSET = 8
EQUIPPED_DETAIL_INSET = 12

# detect_icons fallback: pixels trimmed from each side of a box before the gray
# and edge correlation, and the deeper trim before its silhouette mask.
ICON_GRAY_INSET = 6
ICON_SILHOUETTE_INSET = 12

# mission_references: pixels trimmed from each side of the 87-pixel detail and
# badge mask, excluding frame edges that change independently of the glyph.
MISSION_REFERENCE_INSET = 8

# Mission slot occupancy: width of each edge band read for a colored frame.
# Filled slots have one; empty slots can show the player model through them.
OCCUPANCY_RIM_FRACTION = .096
OCCUPANCY_RIM_MIN = 2

# mission_layout: thickness of the square border stroke, as a fraction of the
# HUD icon size, used to compare outer and inner edge bands.
MISSION_FRAME_BORDER_FRACTION = .057
MISSION_FRAME_BORDER_MIN = 2
