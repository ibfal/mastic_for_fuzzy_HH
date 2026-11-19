"""Group heavy cells into neighborhoods, using only the heavy cells the city already learned
(after Mastic is done), so it reveals nothing new.

1. Heavy cells that touch (share a side or a corner; for H3 a side) are connected, however far the
   chain goes.
2. Connected areas separated by a gap of just one cell are joined, and the cell in the gap becomes
   part of the neighborhood (GAP = 1).
3. Cells enclosed by a neighborhood belong to it: a cell below the threshold surrounded by the
   neighborhood is part of the area, its count just was not revealed. A heavy cell or a smaller
   neighborhood inside such a hole joins the surrounding neighborhood.
4. A neighborhood needs at least MIN_CELLS heavy cells: a lone heavy cell is not one.

Cells added in steps 2 and 3 are below the threshold, so their counts are unknown; they are drawn
as part of the area but add nothing to its pickups. Each neighborhood's border follows the outer
edges of all its cells (`outline`).

Earlier versions tried 5 steps. Chained, that linked the whole centre into one blob (126 of 158
cells); requiring every pair within 5 steps instead split connected areas apart and grouped cells
with gaps between them.

A grid step is one cell of the encoding's own grid:
- Mosaic grid, Morton and geohash are rows and columns, so the distance is the larger of the row
  and column differences (8 neighbours at 1 step).
- H3 is hexagons, so the distance is H3's grid distance (6 neighbours at 1 step).
"""

GAP = 1
MIN_CELLS = 2


def _grid_xy(encoding, bits):
    """(row, column) of a cell of an interleaved encoding."""
    if encoding == "mosaic_grid" or "morton" in encoding:
        # Longitude bit first: lon, lat, lon, ...
        return int(bits[1::2], 2), int(bits[0::2], 2)
    if "geohash" in encoding:
        prefix = encoding["geohash"]["prefix"]
        base32 = "0123456789bcdefghjkmnpqrstuvwxyz"
        full = "".join(f"{base32.index(c):05b}" for c in prefix) + bits
        # Geohash also starts with longitude.
        return int(full[1::2], 2), int(full[0::2], 2)
    raise ValueError(f"no grid for {encoding}")


def _h3(encoding):
    """The h3 module and functions between this encoding's bits and H3 cell ids."""
    import h3  # pip install -r scripts/requirements.txt

    prefix = encoding["h3"]["prefix"]
    resolution = encoding["h3"]["resolution"]
    prefix_res = h3.get_resolution(prefix)
    resolutions = range(prefix_res + 1, resolution + 1)

    def cell(bits):
        # The same reconstruction as scripts/check_heavy_hitters.py `H3.cell`.
        index = h3.str_to_int(prefix)
        index = (index & ~(0xF << 52)) | (resolution << 52)
        for r, i in zip(resolutions, range(0, len(bits), 3)):
            index = (index & ~(7 << 3 * (15 - r))) | (int(bits[i:i + 3], 2) << 3 * (15 - r))
        return h3.int_to_str(index)

    def bits(cell_id):
        index = h3.str_to_int(cell_id)
        return "".join(f"{(index >> 3 * (15 - r)) & 7:03b}" for r in resolutions)

    return h3, cell, bits


def distance_fn(encoding):
    """A function giving the number of grid steps between two cells (by their bits)."""
    if isinstance(encoding, dict) and "h3" in encoding:
        h3, cell, _ = _h3(encoding)

        def dist(a, b):
            try:
                return h3.grid_distance(cell(a), cell(b))
            except Exception:  # Too far apart (or across a pentagon) to measure: not neighbours.
                return float("inf")

        return dist

    def dist(a, b):
        (r0, c0), (r1, c1) = _grid_xy(encoding, a), _grid_xy(encoding, b)
        return max(abs(r0 - r1), abs(c0 - c1))

    return dist


def outline(regions):
    """The border of a group of cells, as line segments [[lat, lon], [lat, lon]]: every cell edge
    that is not shared with another cell of the group. `regions` holds each cell's polygons."""
    def key(p):
        return (round(p[0], 6), round(p[1], 6))

    edges = {}
    for polygons in regions:
        for ring in polygons:
            for a, b in zip(ring, ring[1:] + ring[:1]):
                k = tuple(sorted((key(a), key(b))))
                edges[k] = edges.get(k, 0) + 1
    return [[list(a), list(b)] for (a, b), n in edges.items() if n == 1]


def _geometry(encoding, ref_bits, ref_region):
    """Functions for cells that are not heavy (so have no exported outline): between(a, b), a cell
    touching both a and b when they are 2 steps apart; holes(area), the cells enclosed by a set of
    cells; polygons(bits). Rectangular cells all have the same size, so an outline is the
    reference cell's shifted by whole cells."""
    if isinstance(encoding, dict) and "h3" in encoding:
        h3, cell, bits = _h3(encoding)

        def holes_h3(area):
            # Hexagons have no corner-only neighbours, so the outside spreads to all 6. Start from
            # a ring of cells beyond the farthest area cell, which is certainly outside.
            taken = {cell(b) for b in area}
            centre = next(iter(taken))
            k = max(h3.grid_distance(centre, c) for c in taken) + 1
            disk = set(h3.grid_disk(centre, k))
            outside = {c for c in h3.grid_ring(centre, k) if c not in taken}
            todo = list(outside)
            while todo:
                for nb in h3.grid_disk(todo.pop(), 1):
                    if nb in disk and nb not in taken and nb not in outside:
                        outside.add(nb)
                        todo.append(nb)
            return {bits(c) for c in disk - taken - outside}

        return (
            lambda a, b: bits(h3.grid_path_cells(cell(a), cell(b))[1]),
            holes_h3,
            lambda b: [[list(p) for p in h3.cell_to_boundary(cell(b))]],
        )

    n = len(ref_bits)
    rr, rc = _grid_xy(encoding, ref_bits)
    ring = ref_region[0]
    south, west = min(p[0] for p in ring), min(p[1] for p in ring)
    h, w = max(p[0] for p in ring) - south, max(p[1] for p in ring) - west

    def holes(area):
        # Flood the outside from a corner of the bounding box, through cells that share a side
        # (area cells connect through corners, so the outside must not slip between two diagonal
        # area cells). What the flood does not reach and is not area is enclosed.
        taken = {_grid_xy(encoding, b) for b in area}
        rows, cols = [r for r, _ in taken], [c for _, c in taken]
        r0, r1, c0, c1 = min(rows) - 1, max(rows) + 1, min(cols) - 1, max(cols) + 1
        outside, todo = {(r0, c0)}, [(r0, c0)]
        while todo:
            r, c = todo.pop()
            for nb in ((r + 1, c), (r - 1, c), (r, c + 1), (r, c - 1)):
                if r0 <= nb[0] <= r1 and c0 <= nb[1] <= c1 and nb not in taken and nb not in outside:
                    outside.add(nb)
                    todo.append(nb)
        return {
            _grid_bits(encoding, r, c, n)
            for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)
            if (r, c) not in taken and (r, c) not in outside
        }

    def between(a, b):
        (r0, c0), (r1, c1) = _grid_xy(encoding, a), _grid_xy(encoding, b)
        sign = lambda v: (v > 0) - (v < 0)  # noqa: E731
        return _grid_bits(encoding, r0 + sign(r1 - r0), c0 + sign(c1 - c0), n)

    def polygons(b):
        r, c = _grid_xy(encoding, b)
        s, wst = south + (r - rr) * h, west + (c - rc) * w
        return [[[s, wst], [s, wst + w], [s + h, wst + w], [s + h, wst]]]

    return between, holes, polygons


def _grid_bits(encoding, row, col, n):
    """Inverse of `_grid_xy`: the `n` bits of the cell at (row, col)."""
    if encoding == "mosaic_grid" or "morton" in encoding:
        return "".join(f"{(col >> i) & 1}{(row >> i) & 1}" for i in reversed(range(n // 2)))
    prefix = encoding["geohash"]["prefix"]
    base32 = "0123456789bcdefghjkmnpqrstuvwxyz"
    pre = "".join(f"{base32.index(c):05b}" for c in prefix)
    total = len(pre) + n
    lon_bits, lat_bits = (total + 1) // 2, total // 2
    full = "".join(
        str((col >> (lon_bits - 1 - j // 2)) & 1) if j % 2 == 0
        else str((row >> (lat_bits - 1 - j // 2)) & 1)
        for j in range(total)
    )
    return full[len(pre):]


def neighborhoods(bits_list, regions, encoding, gap=GAP, min_cells=MIN_CELLS):
    """The neighborhoods of the heavy cells `bits_list` (with their `regions`), following the
    rules at the top of this file. Returns, biggest first by heavy cells, one
    (heavy cell indices, {added cell bits: polygons}) per neighborhood."""
    n = len(bits_list)
    if n == 0:
        return []
    dist = distance_fn(encoding)
    between, holes, polygons = _geometry(encoding, bits_list[0], regions[0])
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    d = {(i, j): dist(bits_list[i], bits_list[j]) for i in range(n) for j in range(i + 1, n)}
    # 1. Touching cells.
    for (i, j), v in d.items():
        if v <= 1:
            parent[find(i)] = find(j)
    # 2. Areas separated by a one-cell gap, with a cell in the gap to bridge them.
    bridges = []
    for (i, j), v in d.items():
        if 1 < v <= 1 + gap and find(i) != find(j):
            bridges.append((i, j))
    for i, j in bridges:
        parent[find(i)] = find(j)
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    added = {r: set() for r in groups}
    for i, j in bridges:
        added[find(i)].add(between(bits_list[i], bits_list[j]))
    # 3. Holes, biggest group first so it absorbs any group inside it.
    index = {b: i for i, b in enumerate(bits_list)}
    order = sorted(groups, key=lambda r: -len(groups[r]))
    absorbed, result = set(), []
    for r in order:
        if r in absorbed:
            continue
        members = set(groups[r])
        area = {bits_list[i] for i in members} | added[r]
        for b in holes(area):
            if b in index:  # A heavy cell inside: it joins, and so does its whole group.
                other = find(index[b])
                members |= set(groups[other])
                added[r] |= added[other]
                absorbed.add(other)
            else:
                added[r].add(b)
        # 4. Only groups with enough heavy cells.
        if len(members) >= min_cells:
            extra = {b: polygons(b) for b in added[r] if b not in index}
            result.append((sorted(members), extra))
    result.sort(key=lambda m: -len(m[0]))
    return result
