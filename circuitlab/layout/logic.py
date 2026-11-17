"""Movable digital block geometry; connections remain attached to their nodes."""

from math import copysign, floor, hypot, isfinite


GRID = 10
MAX_COORDINATE = 1_000_000
MAX_NODES, MAX_WIRES, MAX_POINTS = 512, 2048, 128
EPSILON = 1e-6


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and abs(value) <= MAX_COORDINATE and isfinite(value))


def _point(value):
    return (tuple(float(v) for v in value) if isinstance(value, (list, tuple)) and len(value) == 2
            and all(_number(v) for v in value) else None)


def _same(a, b):
    return all(abs(x - y) <= EPSILON for x, y in zip(a, b))


def _snap(value):
    return copysign(GRID * floor(abs(value) / GRID + .5), value)


def _path(values):
    if not isinstance(values, (list, tuple)) or not 2 <= len(values) <= MAX_POINTS:
        return None
    points = [_point(value) for value in values]
    if any(point is None for point in points):
        return None
    if any(abs(a[0] - b[0]) > EPSILON and abs(a[1] - b[1]) > EPSILON for a, b in zip(points, points[1:])):
        return None
    result = []
    for point in points:
        if result and _same(result[-1], point):
            continue
        while len(result) > 1 and (
            abs(result[-2][0] - result[-1][0]) <= EPSILON and abs(result[-1][0] - point[0]) <= EPSILON
            or abs(result[-2][1] - result[-1][1]) <= EPSILON and abs(result[-1][1] - point[1]) <= EPSILON
        ):
            result.pop()
        result.append(point)
    return tuple(result if len(result) > 1 else result * 2)


def _reattach(points, start, end):
    """Keep interior bends, adjusting only the terminal runs where possible."""
    if _same(points[0], start) and _same(points[-1], end):
        return points
    if len(points) == 2:
        horizontal = abs(points[0][1] - points[1][1]) <= EPSILON
        if abs(start[0] - end[0]) <= EPSILON or abs(start[1] - end[1]) <= EPSILON:
            return _path((start, end))
        if horizontal:
            middle = _snap((start[0] + end[0]) / 2)
            return _path((start, (middle, start[1]), (middle, end[1]), end))
        middle = _snap((start[1] + end[1]) / 2)
        return _path((start, (start[0], middle), (end[0], middle), end))
    result = list(points)
    result[0], result[-1] = start, end
    if abs(points[0][1] - points[1][1]) <= EPSILON:
        result[1] = result[1][0], start[1]
    else:
        result[1] = start[0], result[1][1]
    if abs(points[-1][1] - points[-2][1]) <= EPSILON:
        result[-2] = result[-2][0], end[1]
    else:
        result[-2] = end[0], result[-2][1]
    return _path(result)


class LogicGeometry:
    """Grid movement and bounded versioned placement, independent of Qt/source."""

    def __init__(self, rects, routes, record=None):
        if len(rects) > MAX_NODES or len(routes) > MAX_WIRES:
            raise ValueError("Digital layout exceeds its geometry limit.")
        self._rects, self._routes, self._anchors, self._offsets = {}, {}, {}, {}
        for name, rect in rects.items():
            if (not isinstance(name, str) or len(name) > 512 or not isinstance(rect, (list, tuple))
                    or len(rect) != 4 or not all(_number(v) for v in rect) or rect[2] <= 0 or rect[3] <= 0):
                raise ValueError("Use named finite positive block rectangles.")
            self._rects[name] = tuple(float(value) for value in rect)
            self._offsets[name] = [(0, 0), tuple(rect[2:])]
        for edge, values in routes.items():
            path = _path(values)
            if (not isinstance(edge, tuple) or len(edge) != 2 or any(name not in self._rects for name in edge)
                    or path is None):
                raise ValueError("Use orthogonal routes joining known blocks.")
            self._routes[edge] = path
            self._anchors[edge] = tuple((p[0] - self._rects[name][0], p[1] - self._rects[name][1])
                                        for name, p in zip(edge, (path[0], path[-1])))
            for name, anchor in zip(edge, self._anchors[edge]):
                self._offsets[name].append(anchor)
        self.rects, self.routes = dict(self._rects), dict(self._routes)
        if record is not None:
            self.restore(record)

    def snapshot(self):
        return {"version": 1, "nodes": {name: list(rect[:2]) for name, rect in self.rects.items()},
                "wires": [[*edge, [list(point) for point in path]] for edge, path in self.routes.items()]}

    def _ends(self, edge, rects):
        return tuple((rects[name][0] + offset[0], rects[name][1] + offset[1])
                     for name, offset in zip(edge, self._anchors[edge]))

    def _valid_position(self, name, point):
        return all(_number(point[0] + x) and _number(point[1] + y) for x, y in self._offsets[name])

    def _apply(self, rects, routes):
        changed = self.rects != rects or self.routes != routes
        self.rects, self.routes = rects, routes
        return changed

    def reset(self):
        return self._apply(dict(self._rects), dict(self._routes))

    def restore(self, record):
        """Restore known nodes/routes; ignore stale endpoints and malformed data."""
        if record is None:
            return self.reset()
        if not isinstance(record, dict) or record.get("version") != 1:
            return False
        nodes, wires = record.get("nodes", {}), record.get("wires", [])
        if not isinstance(nodes, dict) or len(nodes) > MAX_NODES or not isinstance(wires, list) or len(wires) > MAX_WIRES:
            return False
        rects = dict(self._rects)
        for name, pos in nodes.items():
            if name in rects and (point := _point(pos)) is not None and self._valid_position(name, point):
                rects[name] = (*point, *rects[name][2:])
        routes = {}
        for edge, path in self._routes.items():
            attached = _reattach(path, *self._ends(edge, rects))
            routes[edge] = attached or path
        for wire in wires:
            if not isinstance(wire, (list, tuple)) or len(wire) != 3 or not all(isinstance(v, str) for v in wire[:2]):
                continue
            edge, path = tuple(wire[:2]), _path(wire[2])
            if edge in routes and path is not None:
                start, end = self._ends(edge, rects)
                if _same(path[0], start) and _same(path[-1], end):
                    routes[edge] = path
        return self._apply(rects, routes)

    def move_block(self, name, dx, dy):
        if name not in self.rects or not _number(dx) or not _number(dy):
            return False
        dx, dy = _snap(dx), _snap(dy)
        if not dx and not dy:
            return False
        rects, routes = dict(self.rects), dict(self.routes)
        x, y, width, height = rects[name]
        if not self._valid_position(name, (x + dx, y + dy)):
            return False
        rects[name] = x + dx, y + dy, width, height
        for edge, path in self.routes.items():
            if name not in edge:
                continue
            moved = (_path(tuple((x + dx, y + dy) for x, y in path)) if edge == (name, name)
                     else _reattach(path, *self._ends(edge, rects)))
            if moved is None:
                return False
            routes[edge] = moved
        return self._apply(rects, routes)

    def slide_wire(self, edge, segment_index, delta):
        path = self.routes.get(edge)
        if (path is None or not isinstance(segment_index, int) or isinstance(segment_index, bool)
                or not 0 <= segment_index < len(path) - 1 or not _number(delta)):
            return False
        delta = _snap(delta)
        if not delta or _same(path[segment_index], path[segment_index + 1]):
            return False
        horizontal = abs(path[segment_index][1] - path[segment_index + 1][1]) <= EPSILON
        shifted = list(path)
        for index in (segment_index, segment_index + 1):
            x, y = shifted[index]
            shifted[index] = (x, y + delta) if horizontal else (x + delta, y)
        if segment_index == 0:
            shifted.insert(0, path[0])
        if segment_index == len(path) - 2:
            shifted.append(path[-1])
        moved = _path(shifted)
        if moved is None:
            return False
        routes = dict(self.routes)
        routes[edge] = moved
        return self._apply(dict(self.rects), routes)

    def nearest_segment(self, point, radius):
        point = _point(point)
        if point is None or not _number(radius) or radius < 0:
            return None
        found, distance = None, radius
        for edge, path in self.routes.items():
            for index, (a, b) in enumerate(zip(path, path[1:])):
                if _same(a, b):
                    continue
                near = (min(max(point[0], min(a[0], b[0])), max(a[0], b[0])),
                        min(max(point[1], min(a[1], b[1])), max(a[1], b[1])))
                current = hypot(point[0] - near[0], point[1] - near[1])
                if current <= distance and (found is None or current < distance):
                    found, distance = (edge, index), current
        return found
