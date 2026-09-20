// Canvas rendering primitives.
//
// The splash screen draws with these primitives rather than carrying its own
// copy of them, so the shapes on the title screen are the shapes in the game.
// The arena renderer below extends this file; it does not replace it.

export const CELL = 14;
export const SEGMENT_INSET = 1.5;
export const SEGMENT_RADIUS = 3.5;

// Below this many pixels a cell, eyes are not legible and the gap between
// segments matters more than the shape of them.
export const DETAIL_CELL = 10;

export function insetFor(cell) {
  if (cell >= 14) {
    return SEGMENT_INSET;
  }
  if (cell >= 9) {
    return 1;
  }
  if (cell >= 7) {
    return 0.5;
  }
  return 0;
}

export function radiusFor(cell) {
  return Math.min(SEGMENT_RADIUS, Math.max(1, cell * 0.25));
}

export const PLAYER_COLOURS = [
  '#ef6461', '#f0b950', '#3ecf8e', '#58a6ff', '#c878f0', '#f08fb4',
  '#5b7cfa', '#6fd8d0', '#d9d36a', '#ff9d5c', '#9be86a', '#e0e4ef'
];

// Device pixel ratio aware sizing. Returns the logical width and height.
export function fitCanvas(canvas, ctx) {
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;

  canvas.width = Math.max(1, Math.round(width * ratio));
  canvas.height = Math.max(1, Math.round(height * ratio));
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);

  return { width: width, height: height };
}

export function gridSize(width, height, cell) {
  const size = cell || CELL;
  return {
    cols: Math.max(1, Math.floor(width / size)),
    rows: Math.max(1, Math.floor(height / size)),
    cell: size
  };
}

function roundedRect(ctx, x, y, w, h, radius) {
  const r = Math.min(radius, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r);
  ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

// Draw one segment. Coordinates are in cells and may be fractional, which is
// what lets the splash interpolate and what will later let the arena
// interpolate between ticks.
export function drawSegment(ctx, cellX, cellY, colour, options) {
  const opts = options || {};
  const cell = opts.cell || CELL;
  const inset = opts.inset === undefined ? insetFor(cell) : opts.inset;
  const alpha = opts.alpha === undefined ? 1 : opts.alpha;

  const x = cellX * cell + inset;
  const y = cellY * cell + inset;
  const size = cell - inset * 2;

  ctx.save();
  ctx.globalAlpha = alpha;
  ctx.fillStyle = colour;

  if (opts.glow) {
    ctx.shadowColor = colour;
    ctx.shadowBlur = opts.glow;
  }

  roundedRect(ctx, x, y, size, size, opts.radius || radiusFor(cell));
  ctx.fill();
  ctx.restore();
}

// Lighten a hex colour toward white. Used for the head, so that it reads as
// part of the same snake rather than as a second entity.
function lighten(hex, amount) {
  const value = hex.replace('#', '');
  const full = value.length === 3
    ? value.split('').map(function (c) { return c + c; }).join('')
    : value;

  const channels = [0, 2, 4].map(function (offset) {
    const base = parseInt(full.substring(offset, offset + 2), 16);
    return Math.round(base + (255 - base) * amount);
  });

  return 'rgb(' + channels.join(',') + ')';
}

// Draw the head: a slightly larger, lighter block with two eyes facing the way
// it is travelling. Without this a snake is a row of identical squares and you
// cannot tell which end is which, which matters most in the moment you are
// about to die.
//
// intent is the queued heading. When it differs from the current one the eyes
// look toward it, so a turn pressed mid-move is acknowledged on screen
// immediately instead of a fifth of a second later.
export function drawHead(ctx, cellX, cellY, colour, heading, options) {
  const opts = options || {};
  const cell = opts.cell || CELL;
  const alpha = opts.alpha === undefined ? 1 : opts.alpha;
  const look = opts.intent || heading;

  const inset = Math.max(0, insetFor(cell) - 1);
  const x = cellX * cell + inset;
  const y = cellY * cell + inset;
  const size = cell - inset * 2;

  ctx.save();
  ctx.globalAlpha = alpha;
  ctx.fillStyle = lighten(colour, cell < DETAIL_CELL ? 0.45 : 0.22);
  ctx.shadowColor = colour;
  ctx.shadowBlur = opts.glow === undefined ? 12 : opts.glow;

  roundedRect(ctx, x, y, size, size, radiusFor(cell) + 1);
  ctx.fill();
  ctx.restore();

  // Eyes are a detail, and a detail smaller than about two pixels is noise. On
  // a large arena the head is instead marked by being brighter than the body.
  if (cell < DETAIL_CELL) {
    return;
  }

  // Eyes sit across the direction of travel and forward along it.
  const centreX = cellX * cell + cell / 2;
  const centreY = cellY * cell + cell / 2;
  const forward = cell * 0.17;
  const apart = cell * 0.19;
  const radius = Math.max(1, cell * 0.085);

  const acrossX = look[1];
  const acrossY = look[0];

  ctx.save();
  ctx.globalAlpha = alpha;
  ctx.fillStyle = '#0e1015';

  [-1, 1].forEach(function (side) {
    ctx.beginPath();
    ctx.arc(
      centreX + look[0] * forward + acrossX * apart * side,
      centreY + look[1] * forward + acrossY * apart * side,
      radius,
      0,
      Math.PI * 2
    );
    ctx.fill();
  });

  ctx.restore();
}

// Draw an ordered list of {x, y} segments, head first.
export function drawSnake(ctx, segments, colour, options) {
  const opts = options || {};
  const total = segments.length;
  const alpha = opts.alpha === undefined ? 1 : opts.alpha;

  for (let i = total - 1; i >= 1; i -= 1) {
    const taper = opts.taper === false
      ? 1
      : 0.62 + 0.38 * (1 - i / Math.max(1, total));

    drawSegment(ctx, segments[i].x, segments[i].y, colour, {
      cell: opts.cell,
      inset: opts.inset,
      radius: opts.radius,
      alpha: alpha * taper,
      glow: 0
    });
  }

  if (total > 0 && opts.head !== false) {
    drawHead(ctx, segments[0].x, segments[0].y, colour, opts.heading || [1, 0], {
      cell: opts.cell,
      inset: opts.inset,
      radius: opts.radius,
      alpha: alpha,
      intent: opts.intent,
      glow: opts.headGlow
    });
  } else if (total > 0) {
    drawSegment(ctx, segments[0].x, segments[0].y, colour, {
      cell: opts.cell,
      inset: opts.inset,
      radius: opts.radius,
      alpha: alpha
    });
  }
}

export function lerp(from, to, t) {
  return from + (to - from) * t;
}

export function easeInOutCubic(t) {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

export function easeOutCubic(t) {
  return 1 - Math.pow(1 - t, 3);
}

// ---------------------------------------------------------------------------
// Arena rendering.
//
// The static grid is drawn once to an offscreen canvas and copied in, so only
// the moving parts are redrawn each frame.
// ---------------------------------------------------------------------------

// One palette per arena in a layout, in arena id order: left to right, then top
// to bottom. Colour alone is not enough to tell them apart at a glance for a
// colourblind player, so each carries a background pattern as well, and the
// pattern is the part that survives losing the colour.
// -- palettes ---------------------------------------------------------------
//
// Four sets of four. The outer choice is the theme the host picked; the inner
// one is which arena of a layout you are standing in.
//
// Two things every set has to keep, because the game reads them rather than
// just looking nice:
//
//   The four inside a set must be tellable apart, because on a layout that is
//   how you know which arena you are in and which one lies past an edge.
//   Each carries a background pattern as well as a colour, and the pattern is
//   the part that survives a colourblind player losing the colour.
//
//   The background must stay dark enough for player colours to read against
//   it. The twelve player colours are fixed and chosen against a dark arena,
//   so a light theme would need its own set of them, which is why there is no
//   light theme here.
export const THEME_SETS = {
  // The original, and the baseline the others are pitched against: dark, faint
  // grid, whisper-quiet patterns.
  classic: [
    { name: 'Verdant', background: '#101c14', grid: 'rgba(62,207,142,0.06)',
      border: '#3ecf8e', food: '#f0b950', pattern: 'dots' },
    { name: 'Ember', background: '#1c1410', grid: 'rgba(240,185,80,0.06)',
      border: '#f0b950', food: '#f0b950', pattern: 'hatch' },
    { name: 'Abyss', background: '#101520', grid: 'rgba(88,166,255,0.06)',
      border: '#58a6ff', food: '#f0b950', pattern: 'rings' },
    { name: 'Bloom', background: '#1a1020', grid: 'rgba(200,120,240,0.06)',
      border: '#c878f0', food: '#f0b950', pattern: 'cross' }
  ],

  // Darker than classic and louder on top of it: the arena drops away and the
  // grid and patterns sit up off it. Small, tight pattern spacing.
  neon: [
    { name: 'Acid', background: '#050705', grid: 'rgba(126,249,101,0.16)',
      border: '#7ef965', food: '#fff05e', pattern: 'dots' },
    { name: 'Magenta', background: '#080408', grid: 'rgba(255,92,214,0.16)',
      border: '#ff5cd6', food: '#fff05e', pattern: 'hatch' },
    { name: 'Cyan', background: '#03080a', grid: 'rgba(66,240,255,0.16)',
      border: '#42f0ff', food: '#fff05e', pattern: 'rings' },
    { name: 'Violet', background: '#06040c', grid: 'rgba(157,120,255,0.16)',
      border: '#9d78ff', food: '#fff05e', pattern: 'cross' }
  ],

  // Much lighter than the others, and the only one that reads as a surface
  // rather than as a void. This is the theme that was indistinguishable from
  // classic before: the two differed only in the hue of a near-black, and hue
  // is the weakest signal there is at that lightness.
  earth: [
    { name: 'Moss', background: '#2c3326', grid: 'rgba(214,232,190,0.10)',
      border: '#a8c47e', food: '#ffc95e', pattern: 'dots' },
    { name: 'Clay', background: '#392b23', grid: 'rgba(245,214,190,0.10)',
      border: '#d99366', food: '#ffc95e', pattern: 'hatch' },
    { name: 'Slate', background: '#272f34', grid: 'rgba(206,226,240,0.10)',
      border: '#93b0c4', food: '#ffc95e', pattern: 'rings' },
    { name: 'Heather', background: '#332734', grid: 'rgba(232,206,230,0.10)',
      border: '#bc90b4', food: '#ffc95e', pattern: 'cross' }
  ],

  // One hue at four weights, so the arenas are told apart by brightness and by
  // pattern rather than by colour at all. The set to pick if colour is not
  // carrying information reliably for you.
  mono: [
    // Ordered dimmest to brightest on purpose, and spread far enough apart
    // that the steps are separable. The first version stepped by about 1.5 to
    // 1 between neighbours, which is a difference you can see side by side and
    // not one you can name from memory when you arrive in an arena.
    { name: 'Iron', background: '#0a0a0b', grid: 'rgba(255,255,255,0.05)',
      border: '#4a4a50', food: '#ffffff', pattern: 'cross' },
    { name: 'Ash', background: '#131315', grid: 'rgba(255,255,255,0.07)',
      border: '#7a7a82', food: '#ffffff', pattern: 'dots' },
    { name: 'Stone', background: '#1d1d20', grid: 'rgba(255,255,255,0.09)',
      border: '#b0b0ba', food: '#ffffff', pattern: 'hatch' },
    { name: 'Chalk', background: '#28282c', grid: 'rgba(255,255,255,0.11)',
      border: '#f2f2f6', food: '#ffffff', pattern: 'rings' }
  ]
};

// How loud the background pattern is, and how far apart, per theme.
//
// This is the other half of telling themes apart, and it was missing: the
// pattern was drawn at one fixed alpha and one fixed spacing for every theme,
// so four sets that already shared a near-black background were left with only
// the hue of an accent to distinguish them. Alpha and scale are both here
// because they are different signals -- one changes how present the texture is,
// the other changes what it looks like.
export const THEME_TEXTURE = {
  classic: { alpha: 0.07, scale: 1.0 },
  neon: { alpha: 0.16, scale: 0.7 },
  earth: { alpha: 0.10, scale: 1.6 },
  mono: { alpha: 0.12, scale: 1.0 }
};

export const DEFAULT_THEME_SET = 'classic';

// Which set is in use. A module-level choice rather than an argument threaded
// through every drawing call: it is a property of the match, it changes once
// when a match starts, and passing it to each of the dozen functions that want
// a colour would be a parameter that is the same everywhere it appears.
let current = DEFAULT_THEME_SET;

export function useThemeSet(name) {
  current = THEME_SETS[name] ? name : DEFAULT_THEME_SET;
  return current;
}

export function themeSet() {
  return THEME_SETS[current] || THEME_SETS[DEFAULT_THEME_SET];
}

export function themeSetName() {
  return current;
}

export function themeFor(arenaId) {
  const set = themeSet();
  return set[Math.max(0, arenaId || 0) % set.length];
}

// The pattern is drawn from the arena's own accent at a low alpha, so it reads
// as texture rather than as anything in play. Everything here is in the same
// offscreen canvas as the grid lines and is drawn once per resize.
function drawPattern(ctx, width, height, cell, palette) {
  const kind = palette.pattern;
  if (!kind) {
    return;
  }

  const texture = THEME_TEXTURE[current] || THEME_TEXTURE[DEFAULT_THEME_SET];
  const step = cell * texture.scale;

  ctx.save();
  ctx.strokeStyle = palette.border;
  ctx.fillStyle = palette.border;
  ctx.globalAlpha = texture.alpha;
  ctx.lineWidth = texture.alpha > 0.12 ? 1.5 : 1;

  if (kind === 'dots') {
    const gap = step * 3;
    for (let x = gap; x < width; x += gap) {
      for (let y = gap; y < height; y += gap) {
        ctx.beginPath();
        ctx.arc(x, y, Math.max(0.8, step * 0.09), 0, Math.PI * 2);
        ctx.fill();
      }
    }
  } else if (kind === 'hatch') {
    const gap = step * 2.5;
    ctx.beginPath();
    for (let offset = -height; offset < width; offset += gap) {
      ctx.moveTo(offset, 0);
      ctx.lineTo(offset + height, height);
    }
    ctx.stroke();
  } else if (kind === 'rings') {
    const centreX = width / 2;
    const centreY = height / 2;
    const gap = step * 4;
    const furthest = Math.sqrt(centreX * centreX + centreY * centreY);
    ctx.beginPath();
    for (let radius = gap; radius < furthest; radius += gap) {
      ctx.moveTo(centreX + radius, centreY);
      ctx.arc(centreX, centreY, radius, 0, Math.PI * 2);
    }
    ctx.stroke();
  } else if (kind === 'cross') {
    const gap = step * 2.5;
    ctx.beginPath();
    for (let offset = -height; offset < width; offset += gap) {
      ctx.moveTo(offset, 0);
      ctx.lineTo(offset + height, height);
      ctx.moveTo(offset, height);
      ctx.lineTo(offset + height, 0);
    }
    ctx.stroke();
  }

  ctx.restore();
}

export function createBackground(cols, rows, cell, theme) {
  const palette = theme || themeFor(0);
  const surface = document.createElement('canvas');
  const ratio = window.devicePixelRatio || 1;

  surface.width = Math.round(cols * cell * ratio);
  surface.height = Math.round(rows * cell * ratio);

  const ctx = surface.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);

  ctx.fillStyle = palette.background;
  ctx.fillRect(0, 0, cols * cell, rows * cell);

  ctx.strokeStyle = palette.grid;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let x = 1; x < cols; x += 1) {
    ctx.moveTo(x * cell + 0.5, 0);
    ctx.lineTo(x * cell + 0.5, rows * cell);
  }
  for (let y = 1; y < rows; y += 1) {
    ctx.moveTo(0, y * cell + 0.5);
    ctx.lineTo(cols * cell, y * cell + 0.5);
  }
  ctx.stroke();

  drawPattern(ctx, cols * cell, rows * cell, cell, palette);

  return surface;
}

export function drawFood(ctx, cells, cell, colour) {
  const fill = colour || themeFor(0).food;

  cells.forEach(function (position) {
    const centreX = position[0] * cell + cell / 2;
    const centreY = position[1] * cell + cell / 2;

    ctx.save();
    ctx.fillStyle = fill;
    ctx.shadowColor = fill;
    ctx.shadowBlur = 8;
    ctx.beginPath();
    ctx.arc(centreX, centreY, Math.max(1.6, cell * 0.32), 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  });
}

// Poisons and pickups on the floor.
//
// Each kind has its own shape as well as its own colour. Colour alone would
// mean a colourblind player learning the mechanic by dying to it, and the
// difference that matters most here is not poison from pickup but *which*
// poison, which no palette signals on its own.
//
// All of them are drawn as an outline with a mark inside rather than as a
// filled disc, so nothing on this floor can be mistaken for food at a glance.
// That is the one confusion worth ruling out completely: eating a poison you
// thought was food is the mechanic failing rather than the player.
export const ITEM_STYLE = {
  slow: { colour: '#8fb8ff', mark: 'bars', label: 'Slow' },
  shrink: { colour: '#ff8fa3', mark: 'down', label: 'Shrink' },
  confusion: { colour: '#c78fff', mark: 'swirl', label: 'Confusion' },
  burst: { colour: '#ffd166', mark: 'up', label: 'Burst' },
  phase: { colour: '#7fe7d3', mark: 'ring', label: 'Phase' },
  magnet: { colour: '#9ef07f', mark: 'arc', label: 'Magnet' }
};

export function itemStyle(kind) {
  return ITEM_STYLE[kind] || { colour: '#ffffff', mark: 'ring', label: kind };
}

export function drawItems(ctx, items, cell) {
  items.forEach(function (entry) {
    const style = itemStyle(entry[2]);
    const centreX = entry[0] * cell + cell / 2;
    const centreY = entry[1] * cell + cell / 2;
    const radius = Math.max(2, cell * 0.36);

    ctx.save();
    ctx.strokeStyle = style.colour;
    ctx.fillStyle = style.colour;
    ctx.shadowColor = style.colour;
    ctx.shadowBlur = 6;
    ctx.lineWidth = Math.max(1, cell * 0.09);

    ctx.beginPath();
    ctx.arc(centreX, centreY, radius, 0, Math.PI * 2);
    ctx.stroke();

    const inner = radius * 0.5;
    ctx.beginPath();

    if (style.mark === 'bars') {
      ctx.moveTo(centreX - inner, centreY - inner * 0.5);
      ctx.lineTo(centreX + inner, centreY - inner * 0.5);
      ctx.moveTo(centreX - inner, centreY + inner * 0.5);
      ctx.lineTo(centreX + inner, centreY + inner * 0.5);
      ctx.stroke();
    } else if (style.mark === 'down' || style.mark === 'up') {
      const way = style.mark === 'down' ? 1 : -1;
      ctx.moveTo(centreX, centreY - inner * way);
      ctx.lineTo(centreX, centreY + inner * way);
      ctx.moveTo(centreX - inner * 0.7, centreY + inner * way * 0.3);
      ctx.lineTo(centreX, centreY + inner * way);
      ctx.lineTo(centreX + inner * 0.7, centreY + inner * way * 0.3);
      ctx.stroke();
    } else if (style.mark === 'swirl') {
      ctx.arc(centreX, centreY, inner, 0.4, Math.PI * 1.5);
      ctx.stroke();
    } else if (style.mark === 'arc') {
      ctx.arc(centreX, centreY, inner, Math.PI, Math.PI * 2);
      ctx.stroke();
    } else {
      ctx.arc(centreX, centreY, inner * 0.6, 0, Math.PI * 2);
      ctx.fill();
    }

    ctx.restore();
  });
}

// Interpolate a body between logic moves.
//
// The head slides toward the cell the engine says it is moving into. That cell
// is authoritative: it already accounts for a queued turn and for wrapping, so
// the renderer never predicts a move that does not happen. Predicting from the
// current heading is what made a snake flash across the arena when the player
// turned on the tick that would otherwise have wrapped.
//
// Each other segment slides toward the segment ahead of it. A segment whose
// neighbour is more than one cell away has just wrapped, and is drawn without
// interpolation, because sliding across the whole arena is not what happened.
//
// Returns { body, shift }. shift is the wrap displacement: zero for an ordinary
// move, and one arena width or height when the head is crossing an edge, so the
// caller can draw the incoming half in the right place.
export function interpolateBody(body, heading, next, progress, arena) {
  const drawn = [];
  const head = body[0];

  let stepX = heading[0];
  let stepY = heading[1];
  let shiftX = 0;
  let shiftY = 0;

  if (next && head) {
    const deltaX = next[0] - head[0];
    const deltaY = next[1] - head[1];

    if (arena && Math.abs(deltaX) > 1) {
      // Crossing a vertical edge: travel one cell outward, and the incoming
      // half is one arena width away.
      stepX = deltaX > 0 ? -1 : 1;
      shiftX = deltaX > 0 ? arena.w : -arena.w;
      stepY = 0;
    } else if (arena && Math.abs(deltaY) > 1) {
      stepY = deltaY > 0 ? -1 : 1;
      shiftY = deltaY > 0 ? arena.h : -arena.h;
      stepX = 0;
    } else {
      stepX = deltaX;
      stepY = deltaY;
    }
  }

  for (let i = 0; i < body.length; i += 1) {
    const current = body[i];
    let deltaX;
    let deltaY;

    if (i === 0) {
      deltaX = stepX;
      deltaY = stepY;
    } else {
      deltaX = body[i - 1][0] - current[0];
      deltaY = body[i - 1][1] - current[1];

      if (Math.abs(deltaX) > 1 || Math.abs(deltaY) > 1) {
        deltaX = 0;
        deltaY = 0;
      }
    }

    drawn.push({
      x: current[0] + deltaX * progress,
      y: current[1] + deltaY * progress
    });
  }

  return { body: drawn, shift: { x: shiftX, y: shiftY } };
}

// The same body one arena away, for drawing the half that is coming in while
// the head crosses an edge. Returns null when nothing is crossing.
export function shifted(body, shift) {
  if (!shift || (shift.x === 0 && shift.y === 0)) {
    return null;
  }

  return body.map(function (point) {
    return { x: point.x + shift.x, y: point.y + shift.y };
  });
}

// The whole grid at a glance: which arena you are in, and how many players are
// in each of the others.
//
// Coarse on purpose. It shows a count and never a position, because where
// inside an arena somebody is standing is exactly what a layout exists to hide.
// A count says an arena is worth avoiding without saying which corner of it to
// avoid, and it is also the only thing that survives snapshots being scoped:
// once a client stops being told about snakes it cannot see, it could not
// derive even this for itself.
export function drawMinimap(ctx, options) {
  const arena = options.arena;
  const counts = options.counts || [];
  const width = options.width;
  const height = options.height;

  const tileWidth = width / arena.cols;
  const tileHeight = height / arena.rows;

  ctx.clearRect(0, 0, width, height);

  for (let index = 0; index < arena.cols * arena.rows; index += 1) {
    const column = index % arena.cols;
    const row = Math.floor(index / arena.cols);
    const left = column * tileWidth;
    const top = row * tileHeight;
    const here = index === options.viewArena;
    const palette = themeFor(index);

    ctx.save();
    ctx.fillStyle = palette.background;
    ctx.fillRect(left, top, tileWidth, tileHeight);

    ctx.strokeStyle = palette.border;
    ctx.globalAlpha = here ? 0.9 : 0.35;
    ctx.lineWidth = here ? 2 : 1;
    ctx.strokeRect(
      left + ctx.lineWidth / 2,
      top + ctx.lineWidth / 2,
      tileWidth - ctx.lineWidth,
      tileHeight - ctx.lineWidth
    );
    ctx.restore();

    // One dot per live snake, yours first and in your own colour so the tile
    // you are on says so twice: once in the border and once in the dots.
    const total = counts[index] || 0;
    if (!total) {
      continue;
    }

    const radius = Math.max(1.5, Math.min(3, tileWidth / 12));
    const spacing = radius * 2.8;
    const shown = Math.min(total, 5);
    const start = left + tileWidth / 2 - ((shown - 1) * spacing) / 2;

    for (let dot = 0; dot < shown; dot += 1) {
      const yours = here && options.ownAlive && dot === 0;
      ctx.save();
      ctx.fillStyle = yours ? options.ownColour : 'rgba(255,255,255,0.65)';
      ctx.beginPath();
      ctx.arc(start + dot * spacing, top + tileHeight / 2, radius, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    }
  }
}

export function drawArenaBorder(ctx, cols, rows, cell, colour) {
  ctx.save();
  ctx.strokeStyle = colour || themeFor(0).border;
  ctx.globalAlpha = 0.5;
  ctx.lineWidth = 2;
  ctx.strokeRect(1, 1, cols * cell - 2, rows * cell - 2);
  ctx.restore();
}

// An inward glow at each edge that leads somewhere, tinted with the colour of
// the arena it leads to.
//
// This is the whole of the topology being made legible. A player approaching
// the right edge of a green arena sees amber ahead of them and knows which
// arena they are about to be in before they are in it, without a label, a
// legend or a map.
//
// sides is {left, right, top, bottom}, each a colour or null for an edge that
// leads nowhere.
export function drawEdgeGlow(ctx, cols, rows, cell, sides) {
  const width = cols * cell;
  const height = rows * cell;
  const depth = Math.max(10, cell * 2.5);

  const edges = [
    ['left', 0, 0, depth, 0],
    ['right', width, 0, width - depth, 0],
    ['top', 0, 0, 0, depth],
    ['bottom', 0, height, 0, height - depth]
  ];

  edges.forEach(function (edge) {
    const colour = sides[edge[0]];
    if (!colour) {
      return;
    }

    const gradient = ctx.createLinearGradient(edge[1], edge[2], edge[3], edge[4]);
    gradient.addColorStop(0, colour);
    gradient.addColorStop(1, 'rgba(0,0,0,0)');

    ctx.save();
    ctx.globalAlpha = 0.22;
    ctx.fillStyle = gradient;

    if (edge[0] === 'left') {
      ctx.fillRect(0, 0, depth, height);
    } else if (edge[0] === 'right') {
      ctx.fillRect(width - depth, 0, depth, height);
    } else if (edge[0] === 'top') {
      ctx.fillRect(0, 0, width, depth);
    } else {
      ctx.fillRect(0, height - depth, width, depth);
    }

    ctx.restore();
  });
}

// Walls on the sides that actually are walls. In a layout only the outside of
// the grid is walled; the edges between arenas are open whatever the edge rule
// says, because they are not edges of the space at all.
export function drawWalls(ctx, cols, rows, cell, sides, colour) {
  const width = cols * cell;
  const height = rows * cell;

  ctx.save();
  ctx.strokeStyle = colour || themeFor(0).border;
  ctx.globalAlpha = 0.5;
  ctx.lineWidth = 2;
  ctx.beginPath();

  if (sides.left) {
    ctx.moveTo(1, 0);
    ctx.lineTo(1, height);
  }
  if (sides.right) {
    ctx.moveTo(width - 1, 0);
    ctx.lineTo(width - 1, height);
  }
  if (sides.top) {
    ctx.moveTo(0, 1);
    ctx.lineTo(width, 1);
  }
  if (sides.bottom) {
    ctx.moveTo(0, height - 1);
    ctx.lineTo(width, height - 1);
  }

  ctx.stroke();
  ctx.restore();
}
