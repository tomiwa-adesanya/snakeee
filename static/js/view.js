// Which part of the cell space one player is looking at.
//
// Every coordinate on the wire is in the whole cell space, which on a 2x2
// layout is four arenas wide. A player sees one arena of it, so the board is a
// window: a translation, a palette, and a rule about what lies past each edge.
//
// None of this touches the canvas or the document. It is the arithmetic of the
// window and nothing else, which is what makes it checkable without a browser
// in the way. Everything that actually draws lives in render.js, and everything
// that decides when the window moves lives in match.js.

import { themeFor } from './render.js';

// The arena description from a keyframe, with the fields a single arena does
// not bother to carry filled in. Written once here so nothing downstream has
// to test for them.
export function readArena(payload) {
  return {
    w: payload.w,
    h: payload.h,
    edge: payload.edge,
    aw: payload.aw || payload.w,
    ah: payload.ah || payload.h,
    cols: payload.cols || 1,
    rows: payload.rows || 1
  };
}

export function arenaCount(arena) {
  return arena.cols * arena.rows;
}

export function clampArena(arena, arenaId) {
  return Math.max(0, Math.min(arenaCount(arena) - 1, arenaId || 0));
}

// The top-left cell of an arena, which is what the drawing is translated by.
export function originOf(arena, arenaId) {
  const id = clampArena(arena, arenaId);
  return {
    arena: id,
    x: (id % arena.cols) * arena.aw,
    y: Math.floor(id / arena.cols) * arena.ah
  };
}

function columnRow(arena, arenaId) {
  const id = clampArena(arena, arenaId);
  return { column: id % arena.cols, row: Math.floor(id / arena.cols) };
}

function wrapped(arena, column, row) {
  const c = ((column % arena.cols) + arena.cols) % arena.cols;
  const r = ((row % arena.rows) + arena.rows) % arena.rows;
  return r * arena.cols + c;
}

// Whether a step in a direction leaves the grid altogether, as opposed to
// crossing into the arena next door.
function leavesGrid(arena, place, stepX, stepY) {
  return (stepX < 0 && place.column === 0)
    || (stepX > 0 && place.column === arena.cols - 1)
    || (stepY < 0 && place.row === 0)
    || (stepY > 0 && place.row === arena.rows - 1);
}

// The colour of whatever lies beyond each edge, or null where nothing does.
//
// This is the whole of the topology made legible. Approaching the right edge of
// a green arena you see amber ahead of you and know which arena you are about
// to be in before you are in it, with no label, no legend and no map.
//
// An edge that leads back into this same arena says nothing: a glow tinted with
// the colour you are already standing on is noise. So is an edge that is a
// wall, which has its own line drawn on it instead.
export function edgeColours(arena, arenaId) {
  const place = columnRow(arena, arenaId);
  const here = clampArena(arena, arenaId);

  function beyond(stepX, stepY) {
    if (arena.edge === 'walls' && leavesGrid(arena, place, stepX, stepY)) {
      return null;
    }
    const id = wrapped(arena, place.column + stepX, place.row + stepY);
    return id === here ? null : themeFor(id).border;
  }

  return {
    left: beyond(-1, 0),
    right: beyond(1, 0),
    top: beyond(0, -1),
    bottom: beyond(0, 1)
  };
}

// Which sides of this arena are actually walls. In a layout only the outside of
// the grid is walled: the edges between arenas are open whatever the edge rule
// says, because they are not edges of the space at all.
export function wallSides(arena, arenaId) {
  if (arena.edge !== 'walls') {
    return { left: false, right: false, top: false, bottom: false };
  }

  const place = columnRow(arena, arenaId);

  return {
    left: leavesGrid(arena, place, -1, 0),
    right: leavesGrid(arena, place, 1, 0),
    top: leavesGrid(arena, place, 0, -1),
    bottom: leavesGrid(arena, place, 0, 1)
  };
}

// Where else on this screen the same body has to be drawn.
//
// The space wraps, so a snake crossing the outer edge is at the far side of the
// world and one width from where it belongs on screen. Offsets rather than a
// second copy of the drawing code. Each is rejected by a bounding box test
// before anything is drawn, so the ordinary case costs a few comparisons and
// the crossing case draws twice, which is what a crossing looks like.
export function drawOffsets(arena) {
  const offsets = [[0, 0]];

  if (arena.edge !== 'wrap') {
    return offsets;
  }

  [-1, 0, 1].forEach(function (stepX) {
    [-1, 0, 1].forEach(function (stepY) {
      if (stepX !== 0 || stepY !== 0) {
        offsets.push([stepX * arena.w, stepY * arena.h]);
      }
    });
  });

  return offsets;
}

export function boundsOf(points) {
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;

  points.forEach(function (point) {
    minX = Math.min(minX, point.x);
    maxX = Math.max(maxX, point.x);
    minY = Math.min(minY, point.y);
    maxY = Math.max(maxY, point.y);
  });

  return { minX: minX, maxX: maxX, minY: minY, maxY: maxY };
}

// Whether a body drawn at this offset lands anywhere on the visible board. The
// margin of one cell keeps a body that is halfway through a cell at the edge
// from being dropped a frame before it leaves.
export function visible(arena, origin, bounds, offsetX, offsetY) {
  const left = bounds.minX + offsetX - origin.x;
  const right = bounds.maxX + offsetX - origin.x;
  const top = bounds.minY + offsetY - origin.y;
  const bottom = bounds.maxY + offsetY - origin.y;

  return right >= -1 && left <= arena.aw && bottom >= -1 && top <= arena.ah;
}
