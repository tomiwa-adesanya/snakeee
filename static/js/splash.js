// Splash overlay.
//
// About 5 seconds, then a 400 ms fade to Home. Snakes
// in the twelve player colours enter from random edges, move on the same grid
// the game uses, then converge to trace the title. Any key or click skips.
//
// The title string is never hardcoded here. It arrives from /api/branding, so
// that branding.py stays the only file containing the name .

import {
  CELL,
  PLAYER_COLOURS,
  drawSnake,
  easeInOutCubic,
  easeOutCubic,
  gridSize,
  lerp
} from './render.js';

const ROAM_MS = 3000;
const CONVERGE_MS = 900;
const HOLD_MS = 1100;
const FADE_MS = 400;
const SKIP_FADE_MS = 160;

const SNAKE_COUNT = 12;
const SNAKE_LENGTH = 9;
const STEP_MS = 70;
const TURN_CHANCE = 0.12;

const DIRECTIONS = [
  { x: 1, y: 0 },
  { x: -1, y: 0 },
  { x: 0, y: 1 },
  { x: 0, y: -1 }
];

function randomInt(max) {
  return Math.floor(Math.random() * max);
}

function spawnSnake(grid, colour) {
  const edge = randomInt(4);
  let head;
  let direction;

  if (edge === 0) {
    head = { x: -1, y: randomInt(grid.rows) };
    direction = { x: 1, y: 0 };
  } else if (edge === 1) {
    head = { x: grid.cols, y: randomInt(grid.rows) };
    direction = { x: -1, y: 0 };
  } else if (edge === 2) {
    head = { x: randomInt(grid.cols), y: -1 };
    direction = { x: 0, y: 1 };
  } else {
    head = { x: randomInt(grid.cols), y: grid.rows };
    direction = { x: 0, y: -1 };
  }

  const segments = [];
  for (let i = 0; i < SNAKE_LENGTH; i += 1) {
    segments.push({ x: head.x - direction.x * i, y: head.y - direction.y * i });
  }

  return { colour: colour, direction: direction, segments: segments, targets: null };
}

function stepSnake(snake, grid) {
  let direction = snake.direction;

  if (Math.random() < TURN_CHANCE) {
    const turns = DIRECTIONS.filter(function (candidate) {
      return candidate.x !== -direction.x || candidate.y !== -direction.y;
    });
    direction = turns[randomInt(turns.length)];
  }

  const head = snake.segments[0];
  let next = { x: head.x + direction.x, y: head.y + direction.y };

  // Wrap rather than despawn, so the screen stays populated for the whole roam
  // phase without spawning logic.
  if (next.x < -2) { next.x = grid.cols + 1; }
  if (next.x > grid.cols + 1) { next.x = -2; }
  if (next.y < -2) { next.y = grid.rows + 1; }
  if (next.y > grid.rows + 1) { next.y = -2; }

  snake.direction = direction;
  snake.segments.unshift(next);
  snake.segments.pop();
}

// Rasterise the title into grid cells. Returns an array of {x, y} cells that
// together spell the word at the current canvas size.
//
// Returned one letter at a time rather than as one set of cells, because each
// letter is drawn in its own colour. Grouping by letter is done here, where the
// glyphs are, rather than by slicing the finished word into equal pieces, which
// would put a colour boundary in the middle of a letter.
function lettersIn(text) {
  return text.split('').filter(function (character) {
    return character.trim() !== '';
  });
}

function fitFont(ctx, text, grid) {
  let size = Math.max(
    8, Math.floor(grid.cols / Math.max(4, text.length) * 1.5)
  );

  ctx.font = '700 ' + size + 'px "Space Grotesk", sans-serif';

  let guard = 0;
  while (ctx.measureText(text).width > grid.cols * 0.82 && guard < 40) {
    size -= 1;
    ctx.font = '700 ' + size + 'px "Space Grotesk", sans-serif';
    guard += 1;
  }

  return size;
}

function titleLetters(text, grid) {
  const off = document.createElement('canvas');
  off.width = grid.cols;
  off.height = grid.rows;

  const ctx = off.getContext('2d');
  ctx.fillStyle = '#ffffff';
  ctx.textBaseline = 'middle';

  fitFont(ctx, text, grid);

  // Drawn from the left, one character at a time, so each glyph's own cells can
  // be collected. Advancing by each character's measured width loses kerning,
  // which at one pixel per cell is not a difference anything can see.
  ctx.textAlign = 'left';

  const characters = text.split('');
  const widths = characters.map(function (character) {
    return ctx.measureText(character).width;
  });
  const total = widths.reduce(function (sum, width) {
    return sum + width;
  }, 0);

  let x = (grid.cols - total) / 2;
  const letters = [];

  characters.forEach(function (character, index) {
    const advance = widths[index];

    if (character.trim() === '') {
      x += advance;
      return;
    }

    ctx.clearRect(0, 0, grid.cols, grid.rows);
    ctx.fillText(character, x, grid.rows / 2);
    x += advance;

    const data = ctx.getImageData(0, 0, grid.cols, grid.rows).data;
    const cells = [];

    for (let row = 0; row < grid.rows; row += 1) {
      for (let column = 0; column < grid.cols; column += 1) {
        if (data[(row * grid.cols + column) * 4 + 3] > 110) {
          cells.push({ x: column, y: row });
        }
      }
    }

    letters.push(cells);
  });

  return letters;
}

// Each snake covers part of the one letter it was given at spawn, so a letter
// is one colour however many snakes it takes to draw it.
function assignTargets(snakes, letters) {
  if (letters.length === 0) {
    return false;
  }

  let covered = 0;

  letters.forEach(function (cells, letterIndex) {
    const mine = snakes.filter(function (snake) {
      return snake.letter % letters.length === letterIndex;
    });

    if (mine.length === 0 || cells.length === 0) {
      return;
    }

    // Top to bottom within the letter, so a snake covers a contiguous stroke
    // rather than a scatter of pixels across the whole glyph.
    const ordered = cells.slice().sort(function (a, b) {
      return a.y === b.y ? a.x - b.x : a.y - b.y;
    });

    const perSnake = Math.ceil(ordered.length / mine.length);

    mine.forEach(function (snake, index) {
      const chunk = ordered.slice(index * perSnake, (index + 1) * perSnake);
      snake.targets = chunk;
      snake.origin = [];

      const from = snake.segments;
      for (let i = 0; i < chunk.length; i += 1) {
        const source = from[Math.min(i, from.length - 1)];
        snake.origin.push({ x: source.x, y: source.y });
      }

      covered += chunk.length;
    });
  });

  return covered > 0;
}

export function runSplash(overlay, canvas, title, onDone) {
  const ctx = canvas.getContext('2d');
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  let size = { width: 0, height: 0 };
  let grid = { cols: 1, rows: 1, cell: CELL };
  let snakes = [];
  let converged = false;
  let finished = false;
  let lastStep = 0;
  let start = 0;

  applySize();

  // One colour per letter, so the finished word reads as a row of differently
  // coloured snakes rather than as twelve arbitrary slices of one shape. The
  // colour is decided here rather than when the word is traced, so a snake
  // keeps the same colour while it roams and the word does not change colour
  // as it assembles.
  const letterCount = Math.max(1, lettersIn(title).length);

  for (let i = 0; i < SNAKE_COUNT; i += 1) {
    const letter = i % letterCount;
    const snake = spawnSnake(
      grid, PLAYER_COLOURS[letter % PLAYER_COLOURS.length]
    );
    snake.letter = letter;
    snakes.push(snake);
  }

  function finish(fadeMs) {
    if (finished) {
      return;
    }
    finished = true;

    window.removeEventListener('keydown', skip, true);
    window.removeEventListener('mousedown', skip, true);
    window.removeEventListener('touchstart', skip, true);

    overlay.style.transition = 'opacity ' + fadeMs + 'ms ease';
    overlay.style.opacity = '0';

    window.setTimeout(function () {
      overlay.remove();
      if (typeof onDone === 'function') {
        onDone();
      }
    }, fadeMs);
  }

  function skip(event) {
    if (event && event.key === 'F11') {
      return;
    }
    finish(SKIP_FADE_MS);
  }

  // Sizing is measured, every frame, from the viewport itself.
  //
  // Two earlier attempts at this failed on the GTK WebKit backend: the window
  // resize event, and then a ResizeObserver on the canvas. Both ask something
  // else to notice the change and tell us. The overlay is position: fixed and
  // the canvas is sized in percentages of it, so if that fixed box does not
  // reflow, its width never changes, the observer has nothing to report, and
  // the splash keeps drawing at the size the window had when it opened.
  //
  // This asks the window how big it is and sets the pixels itself, so nothing
  // has to notice anything. It costs two property reads a frame.
  function applySize() {
    const width = Math.max(1, window.innerWidth || canvas.clientWidth || 1);
    const height = Math.max(1, window.innerHeight || canvas.clientHeight || 1);

    if (width === size.width && height === size.height) {
      return false;
    }

    // Set on the overlay too. If the fixed box is the thing that is not
    // reflowing, sizing only the canvas inside it leaves the drawing correct
    // and the box it shows through wrong.
    overlay.style.width = width + 'px';
    overlay.style.height = height + 'px';

    const ratio = window.devicePixelRatio || 1;
    canvas.style.width = width + 'px';
    canvas.style.height = height + 'px';
    canvas.width = Math.max(1, Math.round(width * ratio));
    canvas.height = Math.max(1, Math.round(height * ratio));
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);

    size = { width: width, height: height };
    grid = gridSize(width, height);

    return true;
  }

  function frame(now) {
    if (finished) {
      return;
    }
    if (start === 0) {
      start = now;
      lastStep = now;
    }

    // A resize invalidates the grid the title was traced on, so the word is
    // measured again for the new one rather than being stretched.
    if (applySize()) {
      converged = false;
    }

    const elapsed = now - start;
    ctx.clearRect(0, 0, size.width, size.height);

    if (reduceMotion) {
      drawStaticTitle(elapsed);
    } else if (elapsed < ROAM_MS) {
      if (now - lastStep >= STEP_MS) {
        lastStep = now;
        snakes.forEach(function (snake) {
          stepSnake(snake, grid);
        });
      }
      snakes.forEach(function (snake) {
        drawSnake(ctx, snake.segments, snake.colour, {
          cell: grid.cell,
          heading: [snake.direction.x, snake.direction.y]
        });
      });
    } else {
      if (!converged) {
        converged = assignTargets(snakes, titleLetters(title, grid));
        if (!converged) {
          finish(FADE_MS);
          return;
        }
      }

      const t = Math.min(1, (elapsed - ROAM_MS) / CONVERGE_MS);
      const eased = easeInOutCubic(t);

      snakes.forEach(function (snake) {
        if (!snake.targets || snake.targets.length === 0) {
          return;
        }
        const drawn = [];
        for (let i = 0; i < snake.targets.length; i += 1) {
          drawn.push({
            x: lerp(snake.origin[i].x, snake.targets[i].x, eased),
            y: lerp(snake.origin[i].y, snake.targets[i].y, eased)
          });
        }
        drawSnake(ctx, drawn, snake.colour, {
          cell: grid.cell,
          taper: false,
          head: false
        });
      });

      if (elapsed >= ROAM_MS + CONVERGE_MS + HOLD_MS) {
        finish(FADE_MS);
        return;
      }
    }

    window.requestAnimationFrame(frame);
  }

  function drawStaticTitle(elapsed) {
    const letters = titleLetters(title, grid);
    const alpha = Math.min(1, easeOutCubic(elapsed / 600));

    // The same colour per letter as the animated version, so turning motion off
    // changes the motion and not the design.
    letters.forEach(function (cells, index) {
      const colour = PLAYER_COLOURS[index % PLAYER_COLOURS.length];
      cells.forEach(function (cell) {
        drawSnake(ctx, [cell], colour, {
          cell: grid.cell,
          taper: false,
          head: false,
          alpha: alpha
        });
      });
    });

    if (elapsed > 1800) {
      finish(FADE_MS);
    }
  }

  window.addEventListener('keydown', skip, true);
  window.addEventListener('mousedown', skip, true);
  window.addEventListener('touchstart', skip, true);

  window.requestAnimationFrame(frame);
}
