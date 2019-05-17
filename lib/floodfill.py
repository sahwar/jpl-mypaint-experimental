# This file is part of MyPaint.
# Copyright (C) 2018-2019 by the MyPaint Development Team.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

"""This module implements tile-based floodfill and related operations."""

import logging

import numpy as np

import lib.helpers
import lib.mypaintlib as myplib
import lib.surface
import lib.fill_common as fc
from lib.fill_common import _OPAQUE, _FULL_TILE, _EMPTY_TILE
import lib.morphology

from lib.pycompat import PY3

logger = logging.getLogger(__name__)

TILE_SIZE = N = myplib.TILE_SIZE

# This should point to the array transparent_tile.rgba
# defined in tiledsurface.py
_EMPTY_RGBA = None

# Distance data for tiles with no detected distances
_GAPLESS_TILE = np.full((N, N), 2*N*N, 'uint16')
_GAPLESS_TILE.flags.writeable = False


def is_full(tile):
    """Check if the given tile is the fully opaque alpha tile"""
    return tile is _FULL_TILE


class GapClosingOptions():
    """Container of parameters for gap closing fill operations
    to avoid updates to the callchain in case the parameter set
    is altered.
    """
    def __init__(self, max_gap_size, retract_seeps):
        self.max_gap_size = max_gap_size
        self.retract_seeps = retract_seeps


def orthogonal(tile_coord):
    """ Return the coordinates orthogonal to the input coordinate.

    Return coordinates orthogonal to the input coordinate,
    in the following order:

      0
    3   1
      2
    """
    return fc.nine_grid(tile_coord)[1:5]


def enqueue_overflows(queue, tile_coord, seeds, tiles_bbox, *p):
    """ Conditionally add (coordinate, seed list, data...) tuples to a queue.

    :param queue: the queue which may be appended
    :type queue: list
    :param tile_coord: the 2d coordinate in the middle of the seed coordinates
    :type tile_coord: (int, int)
    :param seeds: 4-tuple of seed lists for n, e, s, w, relative to tile_coord
    :type seeds: (list, list, list, list)
    :param tiles_bbox: the bounding box of the fill operation
    :type tiles_bbox: lib.fill_common.TileBoundingBox
    :param p: tuples of length >= 4, items added to queue items w. same index

    NOTE: This function improves readability significantly in exchange for a
    small performance hit. Replace with explicit queueing if too slow.
    """
    for edge in zip(*(orthogonal(tile_coord), seeds) + p):
        edge_coord = edge[0]
        edge_seeds = edge[1]
        if edge_seeds and not tiles_bbox.outside(edge_coord):
            queue.append(edge)


def starting_coordinates(x, y):
    """Get the coordinates of starting tile and pixel (tx, ty, px, py)"""
    init_tx, init_ty = int(x // N), int(y // N)
    init_x, init_y = int(x % N), int(y % N)
    return init_tx, init_ty, init_x, init_y


def get_target_color(src, tx, ty, px, py):
    """Get the pixel color for the given tile/pixel coordinates"""
    with src.tile_request(tx, ty, readonly=True) as start:
        targ_r, targ_g, targ_b, targ_a = [
            int(c) for c in start[py][px]
        ]
    if targ_a == 0:
        targ_r, targ_g, targ_b = 0, 0, 0

    return targ_r, targ_g, targ_b, targ_a


# Main fill handling function

def flood_fill(
        src, x, y, color, tolerance, offset, feather,
        gap_closing_options, mode, framed, bbox, dst):
    """ Top-level flood fill interface, initiating and delegating actual fill

    :param src: Source surface-like object
    :type src: Anything supporting readonly tile_request()
    :param x: Starting point X coordinate
    :param y: Starting point Y coordinate
    :param color: an RGB color
    :type color: tuple
    :param tolerance: how much filled pixels are permitted to vary
    :type tolerance: float [0.0, 1.0]
    :param offset: the post-fill expansion/contraction radius in pixels
    :type offset: int [-TILE_SIZE, TILE_SIZE]
    :param feather: the amount to blur the fill, after offset is applied
    :type feather: int [0, TILE_SIZE]
    :param gap_closing_options: parameters for gap closing fill, or None
    :type gap_closing_options: lib.floodfill.GapClosingOptions
    :param mode: Fill blend mode - normal, erasing or alpha locked
    :type mode: int (Any of the Combine* modes in mypaintlib)
    :param framed: Whether the frame is enabled or not.
    :type framed: bool
    :param bbox: Bounding box: limits the fill
    :type bbox: lib.helpers.Rect or equivalent 4-tuple
    :param dst: Target surface
    :type dst: lib.tiledsurface.MyPaintSurface

    The fill is performed with reference to src.
    The resulting tiles are composited into dst.
    """

    _, _, w, h = bbox
    if w <= 0 or h <= 0:
        return
    tiles_bbox = fc.TileBoundingBox(bbox)
    del bbox
    # Basic safety clamping
    tolerance = lib.helpers.clamp(tolerance, 0.0, 1.0)
    offset = lib.helpers.clamp(offset, -TILE_SIZE, TILE_SIZE)
    feather = lib.helpers.clamp(feather, 0, TILE_SIZE)

    # Initial parameters
    starting_point = starting_coordinates(x, y)
    r, g, b, a = get_target_color(src, *starting_point)
    filler = myplib.Filler(r, g, b, a, tolerance)

    fill_args = (src, starting_point, tiles_bbox, filler)

    if gap_closing_options:
        fill_args += (gap_closing_options,)
        filled = gap_closing_fill(*fill_args)
    else:
        filled = scanline_fill(*fill_args)

    # Dilate/Erode (Grow/Shrink)
    if offset != 0:
        filled = lib.morphology.morph(offset, filled)

    # Feather (Gaussian blur)
    if feather != 0:
        filled = lib.morphology.blur(feather, filled)

    # When dilating or blurring the fill, only respect the
    # bounding box limits if they are set by an active frame
    trim_result = framed and (offset > 0 or feather != 0)
    composite(mode, color, trim_result, filled, tiles_bbox, dst)


def update_bbox(bbox, tx, ty):
    """Update given the min/max, x/y bounding box
    If a coordinate lies outside of the current
    bounds, set the bounds based on that coordinate
    """
    if bbox:
        min_tx, min_ty, max_tx, max_ty = bbox
        if tx < min_tx:
            min_tx = tx
        elif tx > max_tx:
            max_tx = tx
        if ty < min_ty:
            min_ty = ty
        elif ty > max_ty:
            max_ty = ty
        return min_tx, min_ty, max_tx, max_ty
    else:
        return tx, ty, tx, ty


def composite(mode, fill_col, trim_result, filled, tiles_bbox, dst):
    """Composite the filled tiles into the destination surface"""

    # Prepare opaque color rgba tile for copying
    full_rgba = myplib.rgba_tile_from_alpha_tile(
        _FULL_TILE, *(fill_col + (0, 0, N-1, N-1)))

    # Bounding box of tiles that need updating
    dst_changed_bbox = None
    dst_tiles = dst.get_tiles()

    # Composite filled tiles into the destination surface
    tiles_to_composite = filled.items() if PY3 else filled.iteritems()
    for tile_coord, src_tile in tiles_to_composite:

        # Omit tiles outside of the bounding box _if_ the frame is enabled
        # Note:filled tiles outside bbox only originates from dilation/blur
        if trim_result and tiles_bbox.outside(tile_coord):
            continue

        # Skip empty destination tiles for erasing and alpha locking
        # Avoids completely unnecessary tile allocation and copying
        if mode != myplib.CombineNormal and tile_coord not in dst_tiles:
            continue

        with dst.tile_request(*tile_coord, readonly=False) as dst_tile:

            # Only at this point might the bounding box need to be updated
            dst_changed_bbox = update_bbox(dst_changed_bbox, *tile_coord)

            # Copy full tiles directly if not on the bounding box edge
            # unless the fill is dilated or blurred with no frame set
            cut_off = trim_result and tiles_bbox.crossing(tile_coord)
            if is_full(src_tile) and not cut_off:
                if mode == myplib.CombineNormal:
                    myplib.tile_copy_rgba16_into_rgba16(full_rgba, dst_tile)
                    continue
                elif mode == myplib.CombineDestinationOut:
                    dst_tiles.pop(tile_coord)
                    continue

            # Otherwise, composite the section with provided bounds into the
            # destination tile, most often the entire tile
            if trim_result:
                tile_bounds = tiles_bbox.tile_bounds(tile_coord)
            else:
                tile_bounds = (0, 0, N-1, N-1)
            src_tile_rgba = myplib.rgba_tile_from_alpha_tile(
                src_tile, *(fill_col + tile_bounds))
            myplib.tile_combine(mode, src_tile_rgba, dst_tile, True, 1.0)
    if dst_changed_bbox:
        min_tx, min_ty, max_tx, max_ty = dst_changed_bbox
        bbox = (
            min_tx * N, min_ty * N,
            (1 + max_tx - min_tx) * N,
            (1 + max_ty - min_ty) * N,
        )
        dst.notify_observers(*bbox)


def scanline_fill(src, init, tiles_bbox, filler):
    """ Perform a scanline fill and return the filled tiles

    Perform a scanline fill using the given starting point and tile,
    with reference to the src surface and given bounding box, using the
    provided filler instance.

    :param src: Source surface-like object
    :param init: coordinates for starting tile and pixel
    :type init: (int, int, int, int)
    :param tiles_bbox: Bounding box for the fill
    :type tiles_bbox: lib.fill_common.TileBoundingBox
    :param filler: filler instance performing the per-tile fill operation
    :type filler: mypaintlib.Filler
    :returns: a dictionary of coord->tile mappings for the filled tiles
    """

    # Dict of coord->tile data populated during the fill
    filled = {}

    inv_edges = (
        myplib.edges.south,
        myplib.edges.west,
        myplib.edges.north,
        myplib.edges.east
    )

    # Starting coordinates + direction of origin (from within)
    _tx, _ty, _px, _py = init
    tileq = [((_tx, _ty), (_px, _py), myplib.edges.none)]

    tfs = _TileFillSkipper(tiles_bbox, filler, set({}))

    while len(tileq) > 0:
        tile_coord, seeds, from_dir = tileq.pop(0)
        # Skip if the tile has been fully processed already
        if tile_coord in tfs.final:
            continue
        # Flood-fill one tile
        with src.tile_request(*tile_coord, readonly=True) as src_tile:
            # See if the tile can be skipped
            overflows = tfs.check(tile_coord, src_tile, filled, from_dir)
            if overflows is None:
                if tile_coord not in filled:
                    filled[tile_coord] = np.zeros((N, N), 'uint16')
                overflows = filler.fill(
                    src_tile, filled[tile_coord], seeds,
                    from_dir, *tiles_bbox.tile_bounds(tile_coord)
                )
        enqueue_overflows(tileq, tile_coord, overflows, tiles_bbox, inv_edges)
    return filled


class _TileFillSkipper:
    """Provides checking for, and handling of, uniform tiles"""

    FULL_OVERFLOWS = [
        ((), [(0, N-1)], [(0, N-1)], [(0, N-1)]),         # from north
        ([(0, N-1)], (), [(0, N-1)], [(0, N-1)]),         # from east
        ([(0, N-1)], [(0, N-1)], (), [(0, N-1)]),         # from south
        ([(0, N-1)], [(0, N-1)], [(0, N-1)], ()),         # from west
        ([(0, N-1)], [(0, N-1)], [(0, N-1)], [(0, N-1)])  # from within
    ]

    def __init__(self, tiles_bbox, filler, final):

        self.uniform_tiles = {}
        self.final = final
        self.tiles_bbox = tiles_bbox
        self.filler = filler

    # Dict of alpha->tile, used for uniform non-opaque tile fills
    # NOTE: these are usually not a result of an intentional fill, but
    # clicking a pixel with color very similar to the intended target pixel
    def uniform_tile(self, alpha):
        """ Return a reference to a uniform alpha tile

        If no uniform tile with the given alpha value exists, one is created
        """
        if alpha not in self.uniform_tiles:
            self.uniform_tiles[alpha] = np.full((N, N), alpha, 'uint16')
        return self.uniform_tiles[alpha]

    def check(self, tile_coord, src_tile, filled, from_dir):
        """Check if the tile can be handled without using the fill loop.

        The first time the tile is encountered, check if it is uniform
        and if so, handle it immediately depending on whether it is
        fillable or not.

        If the tile can be handled immediately, returns the overflows
        (new seed ranges), otherwise return None to indicate that the
        fill algorithm needs to be invoked.
        """
        if tile_coord in filled or self.tiles_bbox.crossing(tile_coord):
            return None

        # Returns the alpha of the fill for the tile's color if
        # the tile is uniform, otherwise returns None
        is_empty = src_tile is _EMPTY_RGBA
        alpha = self.filler.tile_uniformity(is_empty, src_tile)

        if alpha is None:
            # No shortcut can be taken, create new tile
            return None
        # Tile is uniform, so there is no need to process
        # it again in the fill loop, either set as
        # a uniformly filled alpha tile or skip it if it
        # cannot be filled at all (unlikely, but not impossible)
        self.final.add(tile_coord)
        if alpha == 0:
            return [(), (), (), ()]
        elif alpha == _OPAQUE:
            filled[tile_coord] = _FULL_TILE
        else:
            filled[tile_coord] = self.uniform_tile(alpha)
        return self.FULL_OVERFLOWS[from_dir]


def gap_closing_fill(src, init, tiles_bbox, filler, gap_closing_options):
    """ Fill loop that finds and uses gap data to avoid unwanted leaks

    Gaps are defined as distances of fillable pixels enclosed on two sides
    by unfillable pixels. Each tile considered, and their neighbours, are
    flooded with alpha values based on the target color and threshold values.
    The resulting alphas are then searched for gaps, and the size of these gaps
    are marked in separate tiles - one for each tile filled.
    """

    full_alphas = {}
    distances = {}
    unseep_q = []
    filled = {}
    final = set({})
    edge = myplib.edges
    const_overflows = [
        [(edge.south,), (edge.west,), (), (edge.east,)],
        [(edge.south,), (edge.west,), (edge.north,), ()],
        [(), (edge.west,), (edge.north,), (edge.east,)],
        [(edge.south,), (), (edge.north,), (edge.east,)],
        [(edge.south,), (edge.west,), (edge.north,), (edge.east,)],
    ]

    options = gap_closing_options
    max_gap_size = lib.helpers.clamp(options.max_gap_size, 1, TILE_SIZE)
    gc_filler = myplib.GapClosingFiller(max_gap_size, options.retract_seeps)
    distbucket = myplib.DistanceBucket(max_gap_size)

    init_tx, init_ty, init_px, init_py = init
    tileq = [((init_tx, init_ty), (init_px, init_py))]

    total_px = 0

    dist_data = None
    while len(tileq) > 0:
        tile_coord, seeds = tileq.pop(0)
        if tile_coord in final:
            continue
        # Pixel limits within tiles vary at the bounding box edges
        px_bounds = tiles_bbox.tile_bounds(tile_coord)
        # Create distance-data and alpha output tiles for the fill
        if tile_coord not in distances:
            # Ensure that alpha data exists for the tile and its neighbours
            prep_alphas(tile_coord, full_alphas, src, filler)
            grid = [full_alphas[ftc] for ftc in fc.nine_grid(tile_coord)]
            if dist_data is None:
                dist_data = np.full((N, N), 2*N*N, 'uint16')
            # Search and mark any gap distances for the tile
            if (
                    all(map(lambda t: t is _FULL_TILE, grid))
                    or not myplib.find_gaps(distbucket, dist_data, *grid)
            ):
                distances[tile_coord] = _GAPLESS_TILE
                # Check if fill can be skipped directly
                if (
                        grid[0] is _FULL_TILE and
                        not tiles_bbox.crossing(tile_coord) and
                        isinstance(seeds, tuple)
                ):
                    final.add(tile_coord)
                    filled[tile_coord] = _FULL_TILE
                    if len(seeds) > 1:
                        out_seeds = const_overflows[edge.none]
                    else:
                        out_seeds = const_overflows[seeds[0]]
                    enqueue_overflows(tileq, tile_coord, out_seeds, tiles_bbox)
                    continue
            else:
                distances[tile_coord] = dist_data
                dist_data = None
            filled[tile_coord] = np.zeros((N, N), 'uint16')
        if isinstance(seeds, tuple) and len(seeds) > 1:
            # Fetch distance for initial seed coord
            dists = distances[tile_coord]
            init_x, init_y = seeds
            init_distance = dists[init_y][init_x]
            # If the fill is starting at a point with a detected distance,
            # disable seep retraction - otherwise it is very likely
            # that the result will be completely empty.
            if init_distance < 2*N*N:
                options.retract_seeps = False
                gc_filler = myplib.GapClosingFiller(
                    max_gap_size, options.retract_seeps
                )
            seeds = [(init_x, init_y, init_distance)]
        # Run the gap-closing fill for the tile
        result = gc_filler.fill(
            full_alphas[tile_coord], distances[tile_coord],
            filled[tile_coord], seeds, *px_bounds)
        overflows = result[0:4]
        enqueue_overflows(tileq, tile_coord, overflows, tiles_bbox)
        fill_edges, px_f = result[4:6]
        # The entire tile was filled; replace data w. constant
        # and mark as final to avoid further processing.
        if px_f == N*N:
            final.add(tile_coord)
            filled[tile_coord] = _FULL_TILE
        total_px += px_f
        if fill_edges:
            unseep_q.append((tile_coord, fill_edges, True))

    # Seep inversion is basically just a four-way 0-alpha fill
    # with different conditions. It only backs off into the original
    # fill and therefore does not require creation of new tiles
    backup = {}
    while len(unseep_q) > 0:
        tile_coord, seeds, is_initial = unseep_q.pop(0)
        if tile_coord not in distances or tile_coord not in filled:
            continue
        if tile_coord not in backup:
            if filled[tile_coord] is _FULL_TILE:
                backup[tile_coord] = _FULL_TILE
                filled[tile_coord] = np.full((N, N), 1 << 15, 'uint16')
            else:
                backup[tile_coord] = np.copy(filled[tile_coord])
        result = gc_filler.unseep(
            distances[tile_coord], filled[tile_coord], seeds, is_initial
        )
        overflows = result[0:4]
        num_erased_pixels = result[4]
        total_px -= num_erased_pixels
        enqueue_overflows(
            unseep_q, tile_coord, overflows, tiles_bbox, (False,)*4
        )
    if total_px <= 0:
        # For small areas, when starting on a distance-marked pixel,
        # backing off may remove the entire fill, in which case we
        # roll back the tiles that were processed
        backup_pairs = backup.items() if PY3 else backup.iteritems()
        for tile_coord, tile in backup_pairs:
            filled[tile_coord] = tile

    return filled


def prep_alphas(tile_coord, full_alphas, src, filler):
    """When needed, create and calculate alpha tiles for distance searching.

    For the tile of the given coordinate, ensure that a corresponding tile
    of alpha values (based on the tolerance function) exists in the full_alphas
    dict for both the tile and all of its neighbors

    """
    for ntc in fc.nine_grid(tile_coord):
        if ntc not in full_alphas:
            with src.tile_request(
                ntc[0], ntc[1], readonly=True
            ) as src_tile:
                is_empty = src_tile is _EMPTY_RGBA
                alpha = filler.tile_uniformity(is_empty, src_tile)
                if alpha == _OPAQUE:
                    full_alphas[ntc] = _FULL_TILE
                elif alpha == 0:
                    full_alphas[ntc] = _EMPTY_TILE
                elif alpha:
                    full_alphas[ntc] = np.full((N, N), alpha, 'uint16')
                else:
                    alpha_tile = np.empty((N, N), 'uint16')
                    filler.flood(src_tile, alpha_tile)
                    full_alphas[ntc] = alpha_tile
