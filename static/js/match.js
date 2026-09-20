// The shared match, as seen by one player.
//
// This module simulates nothing. It sends a desired heading and draws what the
// host reports, exactly as the single player controller does. What it adds is
// everything that follows from the measurement work, and each of the three is
// here for a reason that is not obvious from the code:
//
//   1. The newest state is the only state. A stalled link does not lose frames,
//      it delivers them in a clump, and the Python side folds the clump before
//      this ever sees it. There is no queue here to drain and nothing to catch
//      up on, which is what stops a stall becoming a burst of fast-forward.
//
//   2. The state carries how old it is. On a link that just released a stall,
//      what arrived is already a fifth of a second out of date, so the
//      extrapolation adds that age rather than pretending the state is current.
//
//   3. Your own snake turns when you press, not when the host answers. The host
//      still decides everything: this predicts the queued heading and the cell
//      it leads to, and the moment the host acknowledges the input its answer
//      replaces the guess whether or not the two agree.
//
// The prediction is deliberately narrow. The renderer still slides the head
// toward a cell somebody computed from the geometry rather than toward one
// guessed from a heading, which is the rule that stopped the head flashing
// across the arena. Prediction changes who computed that cell for one snake for
// a fraction of a second; it does not repeal the rule.

import { headingFor, isGameKey } from './input.js';
import { lockScroll, showScreen } from './screens.js';
import {
  createBackground,
  drawEdgeGlow,
  drawFood,
  drawItems,
  drawMinimap,
  drawSnake,
  itemStyle,
  useThemeSet,
  drawWalls,
  interpolateBody,
  themeFor
} from './render.js';
import {
  arenaCount,
  boundsOf,
  clampArena,
  drawOffsets,
  edgeColours,
  originOf,
  readArena,
  visible,
  wallSides
} from './view.js';

const MAX_CELL = 30;
const MIN_CELL = 5;

// Never draw a body more than this far into the next cell. Without the cap, a
// state that arrives late lets extrapolation put a head inside a wall it has
// not hit yet.
const MAX_PROGRESS = 0.94;

// A prediction the host never acknowledges is a prediction about a connection
// that has gone. Dropping it puts the player back on the host's truth rather
// than leaving them steering something imaginary.
const PREDICTION_MAX_MS = 1200;

// How long a press keeps trying to land, and how many times.
//
// The host refuses an input tied to a move the snake has already left, which is
// right: applied where the snake is now it would steer somewhere the player
// never chose. But a refusal used to be the end of the press. The turn was
// already drawn locally, the refusal was acknowledged, the prediction was
// dropped, and the snake straightened back out in front of the person who had
// pressed the key. That is what a stalling link looked like: the game undoing
// your input.
//
// So a refusal is now a retry against the move the host says it is on. The host
// still never applies a heading tied to a stale position; the press just stops
// being thrown away because it arrived at a bad moment. Bounded, because a link
// that cannot land an input in a quarter of a second is not going to, and a
// prediction held past that is a lie about where the snake is.
const RETRY_BUDGET_MS = 260;
const RETRY_LIMIT = 3;

// The staleness readout is a rolling window, and it is deliberately quiet: it
// says nothing at all until the link is doing something a player can feel.
const AGE_SAMPLES = 40;
const AGE_WARN_MS = 120;

const state = {
  active: false,
  elements: {},
  playerId: null,
  amHost: false,
  // Watching rather than playing, and whose stream this is.
  //
  // Kept apart from playerId on purpose. playerId is what the prediction, the
  // input and the "(you)" markers are keyed on, and a spectator has none of
  // those: it must not become the id of the snake being watched, or the board
  // would start predicting turns for somebody else's snake and steering it
  // locally against a host that will never agree.
  spectator: false,
  watching: null,
  arena: null,
  view: { arena: 0, x: 0, y: 0 },
  cell: 14,
  background: null,
  snapshot: null,
  stateAt: 0,
  age: 0,
  ages: [],
  seq: 0,
  prediction: null,
  frame: null,
  pollTimer: null,
  observer: null,
  finished: false,
  onExit: null
};

// -- helpers ----------------------------------------------------------------

async function post(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });

  const payload = await response.json().catch(function () {
    return {};
  });

  return { ok: response.ok, payload: payload };
}

function snakeById(id) {
  const snapshot = state.snapshot;
  if (!snapshot || id === null || id === undefined) {
    return null;
  }
  return (snapshot.snakes || []).find(function (snake) {
    return snake.id === id;
  }) || null;
}

function ownSnake() {
  return snakeById(state.playerId);
}

// The snake this screen is built around: yours, or the one being watched.
//
// Everything that answers "where is the camera, what do the readouts say, whose
// effects are these" asks for this. Everything that answers "whose input is
// this, which row is mine" still asks ownSnake, which is null for a spectator
// and is meant to be.
function focusSnake() {
  return state.spectator ? snakeById(state.watching) : ownSnake();
}

// The same step the engine takes, so a predicted cell is one the host would
// also have produced. JavaScript's remainder keeps the sign of its left operand
// where Python's does not, which is why the wrap is written out rather than
// left to the operator.
function stepFrom(x, y, heading, arena) {
  const nextX = x + heading[0];
  const nextY = y + heading[1];

  if (nextX >= 0 && nextX < arena.w && nextY >= 0 && nextY < arena.h) {
    return [nextX, nextY];
  }

  if (arena.edge === 'wrap') {
    return [
      ((nextX % arena.w) + arena.w) % arena.w,
      ((nextY % arena.h) + arena.h) % arena.h
    ];
  }

  return null;
}

// -- prediction -------------------------------------------------------------

const VECTORS = {
  up: [0, -1],
  down: [0, 1],
  left: [-1, 0],
  right: [1, 0]
};

function clearPrediction() {
  state.prediction = null;
}

// A prediction lives until the host acknowledges the input that caused it, and
// no longer. The acknowledgement is what makes this a prediction rather than a
// second opinion: once it arrives, the host's heading and the host's target
// cell are what get drawn, whether or not they are what was predicted.
function livePrediction(snake, now) {
  const guess = state.prediction;
  if (!guess || !snake || !snake.alive) {
    return null;
  }
  if (now - guess.at > PREDICTION_MAX_MS) {
    clearPrediction();
    return null;
  }
  return guess;
}

// Whether the host has taken the heading this prediction is of.
//
// Compared against the heading rather than trusting the acknowledgement alone,
// because the host acknowledges a sequence number whether it took the input or
// refused it. The client never sends a reversal, having applied that rule
// itself first, so an acknowledged input the host did not take was refused for
// lateness and nothing else.
function predictionSettled(snake, guess) {
  if (typeof snake.ack !== 'number' || snake.ack < guess.seq) {
    return null;
  }
  const taken = snake.ph;
  return Boolean(
    taken && taken[0] === guess.heading[0] && taken[1] === guess.heading[1]
  );
}

// Called when new state arrives, not while drawing. Sending a request from
// inside a render frame would fire it sixty times a second.
function reconcilePrediction(snapshot) {
  const guess = state.prediction;
  if (!guess) {
    return;
  }

  const snake = snakeById(state.playerId);
  if (!snake || !snake.alive || snapshot.phase !== 'running') {
    clearPrediction();
    return;
  }

  const settled = predictionSettled(snake, guess);
  if (settled === null) {
    return;
  }

  if (settled) {
    clearPrediction();
    return;
  }

  const now = window.performance.now();
  const spent = now - guess.at;

  if (guess.tries >= RETRY_LIMIT || spent > RETRY_BUDGET_MS) {
    clearPrediction();
    return;
  }

  // Still legal from where the snake is now? A press that has become a reversal
  // while it was in flight is not resent. Whether a turn is legal depends on
  // where the snake is now, not on what was pressed earlier, and that rule does
  // not stop applying just because this is a retry.
  if (snake.len > 1
      && snake.h[0] === -guess.heading[0]
      && snake.h[1] === -guess.heading[1]) {
    clearPrediction();
    return;
  }

  guess.tries += 1;
  state.seq += 1;
  guess.seq = state.seq;

  post('/api/match/input', {
    heading: guess.name,
    seq: guess.seq,
    move: typeof snake.move === 'number' ? snake.move : 0
  });
}

function intentFor(snake, now) {
  if (snake.id !== state.playerId) {
    return snake.ph;
  }
  const guess = livePrediction(snake, now);
  return guess ? guess.heading : snake.ph;
}

function targetFor(snake, now) {
  if (snake.id !== state.playerId || !state.arena) {
    return snake.n;
  }

  const guess = livePrediction(snake, now);
  if (!guess || !snake.b || !snake.b.length) {
    return snake.n;
  }

  const head = snake.b[0];
  return stepFrom(head[0], head[1], guess.heading, state.arena) || snake.n;
}

// -- input ------------------------------------------------------------------

async function sendHeading(name) {
  const snapshot = state.snapshot;
  const snake = ownSnake();

  // Refused here as well as by the host. A key press from a spectator is not a
  // thing to send and be told off for; there is nothing on the board that is
  // theirs to steer.
  if (state.spectator) {
    return;
  }

  if (!snapshot || snapshot.phase !== 'running' || !snake || !snake.alive) {
    return;
  }

  const pressed = VECTORS[name];
  if (!pressed) {
    return;
  }

  // The same reversal the host applies, applied here so that the prediction is
  // of the heading the press will actually produce. The rule is fixed rather
  // than rolled precisely so that both ends can arrive at it independently: a
  // scrambled remap would mean predicting one turn and being corrected into
  // another, every turn, for the whole six seconds.
  const confused = Boolean(snake.fx && snake.fx.confusion);
  const wanted = confused ? [-pressed[0], -pressed[1]] : pressed;

  // The same refusal the host applies, applied against the heading the host
  // last reported as taken. Checking against the prediction instead would
  // refuse a legal turn: pressing left and then right within one move is two
  // perpendicular turns from where the snake actually is.
  if (snake.len > 1 && snake.h[0] === -wanted[0] && snake.h[1] === -wanted[1]) {
    return;
  }

  const now = window.performance.now();
  state.seq += 1;

  state.prediction = {
    seq: state.seq,
    name: name,
    heading: wanted,
    at: now,
    tries: 0
  };

  const reply = await post('/api/match/input', {
    heading: name,
    seq: state.seq,
    move: typeof snake.move === 'number' ? snake.move : 0
  });

  // Hosting, the answer is the engine's own and arrives with no network in the
  // way. Taking it here is what removes the flash at a wrapping edge: the
  // renderer would otherwise keep sliding toward the cell the previous heading
  // led to until the next poll.
  const answer = reply.payload || {};
  if (answer.ok === false && answer.reason === 'reversal') {
    clearPrediction();
  }
}

function onKeyDown(event) {
  if (!state.active || !isGameKey(event)) {
    return;
  }

  event.preventDefault();

  if (event.repeat) {
    return;
  }

  const heading = headingFor(event);
  if (heading) {
    sendHeading(heading);
  }
}

// -- the view ---------------------------------------------------------------
//
// The geometry of the window lives in view.js. What is here is when it moves
// and what has to be rebuilt when it does.

function setView(arenaId) {
  const arena = state.arena;
  const wanted = clampArena(arena, arenaId);

  if (wanted === state.view.arena && state.background) {
    return;
  }

  state.view = originOf(arena, wanted);

  // The palette and the pattern belong to the arena, so crossing rebuilds
  // them. That happens once per crossing, not once per frame.
  state.background = null;
  sizeCanvas();
}

// The camera follows your own snake, and stays where it is when there is no
// snake to follow: a dead player watches the arena they died in rather than
// being thrown back to the first one.
function updateView() {
  if (!state.arena) {
    return;
  }
  const snake = focusSnake();
  setView(snake && typeof snake.a === 'number' ? snake.a : state.view.arena);
}

// -- sizing -----------------------------------------------------------------

function sizeCanvas() {
  const arena = state.arena;
  const canvas = state.elements.canvas;
  const surface = state.elements.surface;

  if (!arena || !canvas || !surface) {
    return;
  }

  const available = surface.getBoundingClientRect();
  if (available.height < 40 || available.width < 40) {
    return;
  }

  // Sized to one arena, never to the whole grid. A 2x2 layout drawn whole would
  // be four times the cells in the same space, and the point of the layout is
  // that you cannot see the other three.
  const raw = Math.min(available.width / arena.aw, available.height / arena.ah);
  const stepped = Math.floor(raw * 4) / 4;
  const cell = Math.max(MIN_CELL, Math.min(MAX_CELL, stepped));

  state.cell = cell;

  const ratio = window.devicePixelRatio || 1;
  const width = arena.aw * cell;
  const height = arena.ah * cell;

  canvas.style.width = width + 'px';
  canvas.style.height = height + 'px';
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);

  const context = canvas.getContext('2d');
  context.setTransform(canvas.width / width, 0, 0, canvas.height / height, 0, 0);

  state.background = createBackground(
    arena.aw, arena.ah, cell, themeFor(state.view.arena)
  );
}

function watchSurface() {
  if (state.observer || typeof window.ResizeObserver !== 'function') {
    return;
  }

  state.observer = new window.ResizeObserver(function () {
    if (state.active) {
      sizeCanvas();
    }
  });
  state.observer.observe(state.elements.surface);
}

// -- rendering --------------------------------------------------------------

// How far through its current move a snake is. Taken as given.
//
// This used to guess: it took the reported progress and added the time since
// the state arrived, plus how stale that state said it was. Guessing is what
// produced the snapping. A guess is right until it is not, and every time it is
// not the snake is somewhere it should not be and has to be corrected.
//
// The answer now comes already interpolated between two states that actually
// arrived, at a moment slightly in the past, so there is nothing left to guess
// and nothing to correct. Adding an extrapolation back on top here would put
// the guessing straight back in, which is why this reads the number and stops.
function progressFor(snake) {
  const snapshot = state.snapshot;
  if (!snapshot || snapshot.phase !== 'running' || !snake.alive) {
    return 0;
  }
  return Math.min(MAX_PROGRESS, Math.max(0, snake.p));
}

// topLimit is where the top of the visible board is, in the coordinates this
// is being drawn in. A name sits above the head and is pushed down when the
// head is against the top edge, and on a grid the top edge is the top of the
// arena being watched rather than the top of the cell space.
function drawName(ctx, snake, body, cell, topLimit) {
  if (cell < 11 || !body.length) {
    return;
  }

  const head = body[0];
  ctx.save();
  ctx.font = '600 10px system-ui, sans-serif';
  ctx.textAlign = 'center';
  ctx.fillStyle = 'rgba(255,255,255,0.72)';
  ctx.fillText(
    snake.name,
    head.x * cell + cell / 2,
    Math.max((topLimit || 0) + 9, head.y * cell - 3)
  );
  ctx.restore();
}

// The loop runs for as long as the match screen is open, and draws whatever
// there is to draw. It never stops on its own.
//
// It used to return without rescheduling when there was no state yet, which
// made the whole board a race: enterMatch starts polling and then asks for a
// frame, and if the frame arrived before the first answer did, the loop ended
// there and nothing was ever drawn again. Over loopback that is roughly a coin
// toss, which is exactly how it presented: a blank arena on one machine and a
// working one on the other, from the same build, at the same moment.
function render(now) {
  if (!state.active) {
    state.frame = null;
    return;
  }

  state.frame = window.requestAnimationFrame(render);
  pumpState();

  const canvas = state.elements.canvas;
  if (!canvas || !state.arena) {
    return;
  }

  // The board can be measured before the screen it lives on has a height, in
  // which case sizing gives up and waits. Retrying from here means it recovers
  // on the next frame instead of depending on a resize that may never come.
  if (!state.background) {
    sizeCanvas();
    if (!state.background) {
      return;
    }
  }

  const ctx = canvas.getContext('2d');
  const arena = state.arena;
  const cell = state.cell;
  const width = arena.aw * cell;
  const height = arena.ah * cell;

  ctx.clearRect(0, 0, width, height);

  if (state.background) {
    ctx.drawImage(state.background, 0, 0, width, height);
  }

  if (arenaCount(arena) > 1) {
    drawEdgeGlow(
      ctx, arena.aw, arena.ah, cell, edgeColours(arena, state.view.arena)
    );
  }

  const offsets = drawOffsets(arena);
  const originX = state.view.x;
  const originY = state.view.y;

  const snapshot = state.snapshot;
  if (snapshot) {
    // Food in the other arenas is somebody else's problem and is not drawn.
    const food = (snapshot.food || []).filter(function (position) {
      return position[0] >= originX && position[0] < originX + arena.aw
        && position[1] >= originY && position[1] < originY + arena.ah;
    });

    const items = (snapshot.items || []).filter(function (entry) {
      return entry[0] >= originX && entry[0] < originX + arena.aw
        && entry[1] >= originY && entry[1] < originY + arena.ah;
    });

    ctx.save();
    ctx.translate(-originX * cell, -originY * cell);
    drawFood(ctx, food, cell);
    drawItems(ctx, items, cell);
    ctx.restore();

    (snapshot.snakes || []).forEach(function (snake) {
      if (!snake.b || !snake.b.length) {
        return;
      }

      const mine = snake.id === state.playerId;
      const step = interpolateBody(
        snake.b,
        snake.h,
        targetFor(snake, now),
        progressFor(snake, now),
        arena
      );

      // Spawn protection is a rule with consequences, so it is visible rather
      // than something a player works out by dying and not dying.
      const pulse = snake.protected
        ? 0.45 + 0.35 * Math.abs(Math.sin(now / 160))
        : 1;

      const options = {
        cell: cell,
        heading: snake.h,
        intent: intentFor(snake, now),
        alpha: snake.alive ? pulse : 0.35,
        headGlow: mine ? 18 : 10
      };

      // Drawn wherever it lands on this screen, which is usually once and is
      // twice while it is crossing the outer edge. The half that has wrapped
      // and the half that has not are the same body an arena apart, which is
      // what the offsets are.
      const bounds = boundsOf(step.body);
      offsets.forEach(function (offset) {
        if (!visible(arena, state.view, bounds, offset[0], offset[1])) {
          return;
        }

        ctx.save();
        ctx.translate(
          (offset[0] - originX) * cell,
          (offset[1] - originY) * cell
        );
        drawSnake(ctx, step.body, snake.c, options);
        drawName(
          ctx, snake, step.body, cell, (originY - offset[1]) * cell
        );
        ctx.restore();
      });
    });
  }

  drawWalls(ctx, arena.aw, arena.ah, cell, wallSides(arena, state.view.arena));
}

// -- the readouts -----------------------------------------------------------

function renderBoard(snapshot) {
  const list = state.elements.board;
  list.textContent = '';

  (snapshot.standings || []).forEach(function (entry, index) {
    const row = document.createElement('div');
    row.className = 'standing'
      + (entry.id === state.playerId ? ' is-you' : '')
      + (entry.eliminated ? ' is-out' : '');

    const rank = document.createElement('span');
    rank.className = 'standing-rank mono';
    rank.textContent = index + 1;
    row.appendChild(rank);

    const dot = document.createElement('span');
    dot.className = 'player-dot';
    dot.style.background = entry.colour;
    row.appendChild(dot);

    const name = document.createElement('span');
    name.className = 'standing-name';
    name.textContent = entry.username;
    row.appendChild(name);

    // Only when there is more than one arena to be in. On a single arena the
    // column would be the same colour on every row, every match.
    if (state.arena && arenaCount(state.arena) > 1) {
      const where = document.createElement('span');
      const palette = themeFor(entry.arena || 0);
      where.className = 'standing-arena';
      where.style.background = palette.border;
      where.title = palette.name;
      row.appendChild(where);
    }

    const score = document.createElement('span');
    score.className = 'standing-score mono';
    score.textContent = entry.score;
    row.appendChild(score);

    const kills = document.createElement('span');
    kills.className = 'standing-kills mono';
    kills.textContent = entry.kills + 'k';
    row.appendChild(kills);

    const lives = document.createElement('span');
    lives.className = 'standing-lives mono';
    lives.textContent = entry.eliminated
      ? 'out'
      : (entry.lives > 0 ? entry.lives + ' left' : 'alive');
    row.appendChild(lives);

    list.appendChild(row);
  });
}

// A median next to a tail is what hid the original latency problem for weeks,
// so what is reported here is the tail. It stays hidden while the link is
// behaving, because a number that is always on screen and always fine is a
// number nobody reads on the day it stops being fine.
function renderLink() {
  const samples = state.ages;
  if (samples.length < 10) {
    state.elements.link.hidden = true;
    return;
  }

  const ordered = samples.slice().sort(function (a, b) {
    return a - b;
  });
  const tail = ordered[Math.min(ordered.length - 1, Math.floor(ordered.length * 0.95))];

  if (tail < AGE_WARN_MS) {
    state.elements.link.hidden = true;
    return;
  }

  state.elements.link.hidden = false;

  // Milliseconds behind is the honest number; cells jumped is the one that
  // describes what is on the screen. Both, when there is a jump to report.
  // Said in the terms the player can act on. The delay is the smoothing this
  // link is costing them; a jump is smoothing that could not cover it.
  const snapshot = state.snapshot;
  const jump = snapshot && typeof snapshot.jump === 'number' ? snapshot.jump : 0;
  const delay = snapshot && typeof snapshot.delay_ms === 'number'
    ? snapshot.delay_ms
    : 0;

  state.elements.link.textContent = jump > 1
    ? 'Link stalling, up to ' + tail + 'ms behind, snakes jumping '
      + jump + ' cells'
    : 'Link uneven, smoothing over ' + delay + 'ms';
}

// What just happened, in words. Severing is otherwise invisible: you are
// suddenly half as long and nothing on the screen says who did it or whether
// you did it to yourself.
//
// The host prunes the list and sends what survives with every message, so this
// draws exactly what it is given and keeps no list of its own. The only local
// state is which ids have already been rendered, so an entry that is still in
// the window is not rebuilt and restarted every fiftieth of a second.
const EVENT_LINES = 3;

const shownEvents = new Map();

// The arena an event happened in, as words, or nothing when there is only one
// arena or the host did not say. Appended rather than led with: who did what to
// whom is the sentence, and where is the footnote.
function whereOf(event) {
  if (!state.arena || arenaCount(state.arena) < 2) {
    return null;
  }
  if (typeof event.w !== 'number') {
    return null;
  }
  const palette = themeFor(event.w);
  return [' in ' + palette.name, palette.border, 'event-where'];
}

function describe(event) {
  if (event.k === 'sever') {
    if (!event.a) {
      return [[event.t, event.tc], [' cut its own tail, -' + event.n, null]];
    }
    return [
      [event.a, event.ac],
      [' cut ', null],
      [event.t, event.tc],
      [' by ' + event.n, null]
    ];
  }

  if (event.k === 'out') {
    return [[event.t, event.tc], [' is out', null]];
  }

  if (event.k === 'death') {
    if (event.a) {
      const verb = event.c === 'severed' ? ' cut down ' : ' killed ';
      return [[event.a, event.ac], [verb, null], [event.t, event.tc]];
    }
    const how = {
      wall: ' hit the wall',
      self: ' ran into itself',
      head_on: ' went head on',
      severed: ' was cut too short',
      snake: ' ran into a snake'
    }[event.c] || ' died';
    return [[event.t, event.tc], [how, null]];
  }

  return null;
}

function renderEvents(snapshot) {
  const feed = state.elements.events;
  const events = (snapshot.events || []).slice(-EVENT_LINES);
  const live = new Set();

  events.forEach(function (event) {
    live.add(event.id);

    if (shownEvents.has(event.id)) {
      return;
    }

    const parts = describe(event);
    if (!parts) {
      return;
    }

    const where = whereOf(event);
    if (where) {
      parts.push(where);
    }

    const line = document.createElement('p');
    line.className = 'event-line';

    parts.forEach(function (part) {
      const span = document.createElement('span');
      span.textContent = part[0];
      if (part[1]) {
        span.className = part[2] || 'event-name';
        span.style.color = part[1];
      }
      line.appendChild(span);
    });

    feed.appendChild(line);
    shownEvents.set(event.id, line);
  });

  shownEvents.forEach(function (line, id) {
    if (live.has(id)) {
      return;
    }
    line.remove();
    shownEvents.delete(id);
  });
}

// Sized from the layout so a 2x1 is a wide strip and a 2x2 is a square, and
// only ever drawn when there is more than one arena to draw.
// What is acting on you and how long is left of it.
//
// Yours only. Everybody's would be a wall of text, and what somebody else is
// carrying is readable from how they are playing.
function renderEffects() {
  const holder = state.elements.effects;
  if (!holder) {
    return;
  }

  const mine = focusSnake();
  const active = mine && mine.fx ? mine.fx : {};
  const kinds = Object.keys(active).sort();

  if (!kinds.length) {
    holder.hidden = true;
    holder.textContent = '';
    return;
  }

  holder.hidden = false;
  holder.textContent = '';

  kinds.forEach(function (kind) {
    const style = itemStyle(kind);

    const row = document.createElement('div');
    row.className = 'effect';

    const dot = document.createElement('span');
    dot.className = 'effect-dot';
    dot.style.background = style.colour;
    row.appendChild(dot);

    const name = document.createElement('span');
    name.textContent = style.label;
    row.appendChild(name);

    const left = document.createElement('span');
    left.className = 'effect-left mono';
    left.textContent = (active[kind] / 1000).toFixed(1) + 's';
    row.appendChild(left);

    holder.appendChild(row);
  });
}

function renderMinimap(snapshot) {
  const holder = state.elements.minimap;
  const canvas = state.elements.minimapCanvas;
  const arena = state.arena;

  if (!holder || !canvas) {
    return;
  }

  if (!arena || arenaCount(arena) < 2) {
    holder.hidden = true;
    return;
  }

  holder.hidden = false;

  const width = 132;
  const height = Math.round(
    width * (arena.rows * arena.ah) / (arena.cols * arena.aw)
  );

  const ratio = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(width * ratio)
      || canvas.height !== Math.round(height * ratio)) {
    canvas.style.width = width + 'px';
    canvas.style.height = height + 'px';
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
  }

  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);

  const mine = focusSnake();

  drawMinimap(ctx, {
    arena: arena,
    viewArena: state.view.arena,
    counts: snapshot.map || [],
    ownColour: mine ? mine.c : '#ffffff',
    ownAlive: Boolean(mine && mine.alive),
    width: width,
    height: height
  });
}

// Who a spectator is following, and the other players it could follow instead.
//
// Drawn from the standings rather than from the lobby, so it lists exactly the
// snakes in this match: a player who left mid-match is not offered, and one who
// joined mid-match is.
function renderWatching(snapshot) {
  const holder = state.elements.watching;
  const choices = state.elements.watchChoices;

  if (!holder || !choices) {
    return;
  }

  if (!state.spectator) {
    holder.hidden = true;
    return;
  }

  holder.hidden = false;
  choices.textContent = '';

  (snapshot.standings || []).forEach(function (entry) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'watch-choice'
      + (entry.id === state.watching ? ' is-watched' : '');
    button.disabled = entry.id === state.watching;

    const dot = document.createElement('span');
    dot.className = 'player-dot';
    dot.style.background = entry.colour;
    button.appendChild(dot);

    const name = document.createElement('span');
    name.textContent = entry.username;
    button.appendChild(name);

    button.addEventListener('click', function () {
      watch(entry.id);
    });

    choices.appendChild(button);
  });
}

function renderHud(snapshot) {
  const snake = focusSnake();

  state.elements.score.textContent = snake ? snake.score : 0;
  state.elements.kills.textContent = snake ? snake.kills : 0;
  state.elements.length.textContent = snake ? snake.len : 0;
  state.elements.lives.textContent = snake
    ? (snake.eliminated ? 'out' : (snake.lives > 0 ? snake.lives : '--'))
    : '--';

  const remaining = snapshot.remaining_seconds;
  state.elements.clock.hidden = remaining === null || remaining === undefined;
  if (!state.elements.clock.hidden) {
    const minutes = Math.floor(remaining / 60);
    const seconds = remaining % 60;
    state.elements.clockValue.textContent =
      minutes + ':' + String(seconds).padStart(2, '0');
  }

  renderWatching(snapshot);
  renderBoard(snapshot);
  renderEffects();
  renderMinimap(snapshot);
  renderEvents(snapshot);
  renderLink();
}

function renderOverlay(snapshot) {
  const snake = ownSnake();
  const overlay = state.elements.overlay;

  if (snapshot.phase === 'countdown') {
    overlay.hidden = false;
    const seconds = Math.ceil((snapshot.countdown_ms || 0) / 1000);
    state.elements.overlayTitle.textContent = seconds > 0 ? seconds : 'Go';
    state.elements.overlayNote.textContent = state.spectator
      ? 'Watching.'
      : 'Arrows or WASD to steer.';
    return;
  }

  // The rest of this is about your own snake being dead or out, and a spectator
  // has none. Without this it would read the null and show nothing, which is
  // the right picture by accident rather than on purpose.
  if (state.spectator) {
    overlay.hidden = true;
    return;
  }

  if (snake && snake.eliminated) {
    overlay.hidden = false;
    state.elements.overlayTitle.textContent = 'Out';
    state.elements.overlayNote.textContent = 'You are out of lives. Watching '
      + 'until the match ends.';
    return;
  }

  if (snake && !snake.alive && snake.respawn_in !== null
      && snake.respawn_in !== undefined) {
    overlay.hidden = false;
    state.elements.overlayTitle.textContent =
      Math.ceil(snake.respawn_in / 1000) || 1;
    state.elements.overlayNote.textContent = 'Back in a moment.';
    return;
  }

  overlay.hidden = true;
}

function showResults(snapshot) {
  const outcome = snapshot.outcome || {};

  const reason = {
    last_standing: 'Everyone else is out.',
    everyone_out: 'Nobody is left standing.',
    score_target: 'The score target was reached.',
    kill_target: 'The kill target was reached.',
    time_limit: 'Time is up.',
    closed: 'The host ended the match.',
    engine_error: 'The match stopped because of an error on the host.'
  }[outcome.reason] || 'The match is over.';

  state.elements.resultTitle.textContent = outcome.winner === state.playerId
    ? 'You win'
    : (outcome.winner_name ? outcome.winner_name + ' wins' : 'No winner');
  state.elements.resultReason.textContent = reason;
  state.elements.resultDuration.textContent =
    (outcome.duration_seconds || 0) + 's';

  const list = state.elements.resultBoard;
  list.textContent = '';

  (outcome.standings || snapshot.standings || []).forEach(function (entry, index) {
    const row = document.createElement('div');
    row.className = 'standing' + (entry.id === state.playerId ? ' is-you' : '');

    [
      String(index + 1),
      entry.username,
      entry.score + ' pts',
      entry.kills + ' kills',
      entry.deaths + ' deaths'
    ].forEach(function (text, column) {
      const cell = document.createElement('span');
      cell.className = column === 1 ? 'standing-name' : 'standing-score mono';
      cell.textContent = text;
      row.appendChild(cell);
    });

    list.appendChild(row);
  });

  state.elements.rematch.hidden = !state.amHost;
  state.elements.waiting.hidden = state.amHost;

  state.elements.results.hidden = false;
  lockScroll(true);
}

// -- transport --------------------------------------------------------------

function applyState(payload) {
  if (!payload || payload.phase === 'none') {
    // Not a reason to leave. A player who has just joined reports no match
    // until their first keyframe lands, which is a fraction of a second in
    // which this screen has nothing to draw and nothing to conclude. Leaving
    // the match is driven by the room going back to waiting, which is one
    // decision made in one place rather than two racing each other.
    return;
  }

  state.snapshot = payload;
  state.stateAt = window.performance.now();
  state.age = typeof payload.age_ms === 'number' ? payload.age_ms : 0;

  // Sampled only while the match is actually being sent. A finished match stops
  // broadcasting, so the newest state keeps ageing with nothing to replace it,
  // and the readout climbs for as long as the results screen is open. Three
  // seconds behind is a true statement about a stream that ended and a false
  // one about the connection.
  if (payload.phase === 'running') {
    state.ages.push(state.age);
    if (state.ages.length > AGE_SAMPLES) {
      state.ages.shift();
    }
  } else {
    state.ages = [];
  }

  if (!state.arena && payload.arena) {
    // The theme is set before the first frame is drawn, because it decides
    // every colour on the board including the background, which is built once
    // and reused. It comes from the rules the host sent rather than from
    // anything local: everybody in a room sees the same arena.
    useThemeSet((payload.rules || {}).theme_set);

    state.arena = readArena(payload.arena);
    state.background = null;
    window.requestAnimationFrame(sizeCanvas);
  }

  // Every state, not only the first: this is what moves the board to the next
  // arena at the moment your own head arrives in it.
  updateView();

  reconcilePrediction(payload);
  renderHud(payload);
  renderOverlay(payload);

  if (payload.phase === 'over' && !state.finished) {
    state.finished = true;
    clearPrediction();
    showResults(payload);
  }
}

async function poll() {
  try {
    const response = await fetch('/api/match/state', { cache: 'no-store' });
    if (response.ok) {
      applyState(await response.json());
    }
  } catch (error) {
    console.error('match poll failed', error);
  }
}

// One request per drawn frame rather than a timer of its own.
//
// The two used to run independently, the state arriving twenty times a second
// and the screen drawing sixty, so a change could wait up to a frame to be
// drawn on top of everything else it was already waiting for. Asking from
// inside the loop costs nothing extra over loopback and removes that wait
// entirely: every frame draws the newest answer there is.
//
// Guarded so a slow answer cannot pile requests up behind it.
function pumpState() {
  if (state.fetching || !state.active) {
    return;
  }
  state.fetching = true;
  poll().finally(function () {
    state.fetching = false;
  });
}

function startPolling() {
  stopPolling();
  state.fetching = false;
  poll();
}

function stopPolling() {
  state.fetching = false;
  if (state.pollTimer) {
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

// -- lifecycle --------------------------------------------------------------

export function enterMatch(options) {
  state.playerId = options.playerId;
  state.amHost = Boolean(options.amHost);
  state.spectator = Boolean(options.spectator);
  state.watching = options.spectator ? options.watching : null;
  state.onExit = options.onExit;

  state.active = true;
  state.finished = false;
  state.snapshot = null;
  state.arena = null;
  state.view = { arena: 0, x: 0, y: 0 };
  state.background = null;
  state.ages = [];
  state.seq = 0;
  clearPrediction();

  state.elements.results.hidden = true;
  state.elements.overlay.hidden = true;
  if (state.elements.controlsNote) {
    state.elements.controlsNote.textContent = state.spectator
      ? 'Watching. Pick a player above to follow. F11 fullscreen.'
      : 'Arrows or WASD to steer. F11 fullscreen.';
  }
  lockScroll(false);

  showScreen('match');
  watchSurface();
  startPolling();

  if (!state.frame) {
    state.frame = window.requestAnimationFrame(render);
  }

  state.elements.canvas.focus();
}

export function exitMatch() {
  if (!state.active) {
    return;
  }

  state.active = false;
  stopPolling();

  if (state.frame) {
    window.cancelAnimationFrame(state.frame);
    state.frame = null;
  }

  state.snapshot = null;
  state.arena = null;
  state.view = { arena: 0, x: 0, y: 0 };
  state.background = null;
  state.ages = [];
  clearPrediction();

  state.elements.events.textContent = '';
  state.spectator = false;
  state.watching = null;
  if (state.elements.watching) {
    state.elements.watching.hidden = true;
    state.elements.watchChoices.textContent = '';
  }
  if (state.elements.minimap) {
    state.elements.minimap.hidden = true;
  }
  if (state.elements.effects) {
    state.elements.effects.hidden = true;
    state.elements.effects.textContent = '';
  }
  shownEvents.clear();

  state.elements.results.hidden = true;
  state.elements.overlay.hidden = true;
  state.elements.link.hidden = true;
  lockScroll(false);

  // Back to the screen this was entered from. Leaving the board showing is
  // what made a finished match impossible to get out of: the leave request
  // went through and the room really did close, but the player was still
  // looking at an empty arena with the navigation hidden, so nothing they
  // pressed appeared to do anything.
  showScreen('multi');

  if (state.onExit) {
    state.onExit();
  }
}

export function matchIsActive() {
  return state.active;
}

// Ask the host to move this spectator onto another player's stream.
//
// Nothing is changed locally. The host answers with a lobby update, the room
// poll reads the new target out of it and calls setWatching, so which stream
// this screen believes it is on is always the host's answer and never a hope.
async function watch(playerId) {
  if (!state.spectator || playerId === state.watching) {
    return;
  }
  await post('/api/room/watch', { player: playerId });
}

// Told by the room poll, every poll. A spectator can be moved by the host
// without asking, when the player it was following leaves the room.
export function setWatching(playerId) {
  if (!state.spectator || playerId === state.watching) {
    return;
  }
  state.watching = playerId === undefined ? null : playerId;

  // The camera belongs to the snake being followed, and the new one may be in
  // another arena. Letting the next state move it would leave one frame drawn
  // with the old arena's palette around the new arena's snakes.
  updateView();
}

async function rematch() {
  await post('/api/room/rematch');
  // The exit itself is left to the next poll, which will find no match. Doing
  // it here as well would race with it and leave the screen half torn down.
}

async function leave() {
  await post('/api/room/leave');
  exitMatch();
}

export function initMatch(root) {
  const panel = root.querySelector('[data-screen="match"]');
  if (!panel) {
    return;
  }

  state.elements = {
    surface: panel.querySelector('.board-surface'),
    canvas: panel.querySelector('[data-match-canvas]'),
    score: panel.querySelector('[data-match-score]'),
    kills: panel.querySelector('[data-match-kills]'),
    length: panel.querySelector('[data-match-length]'),
    lives: panel.querySelector('[data-match-lives]'),
    clock: panel.querySelector('[data-match-clock]'),
    clockValue: panel.querySelector('[data-match-clock-value]'),
    board: panel.querySelector('[data-match-board]'),
    link: panel.querySelector('[data-match-link]'),
    events: panel.querySelector('[data-match-events]'),
    effects: panel.querySelector('[data-match-effects]'),
    watching: panel.querySelector('[data-match-watching]'),
    watchChoices: panel.querySelector('[data-match-watch-choices]'),
    controlsNote: panel.querySelector('[data-match-controls-note]'),
    minimap: panel.querySelector('[data-match-minimap]'),
    minimapCanvas: panel.querySelector('[data-match-minimap-canvas]'),
    overlay: panel.querySelector('[data-match-overlay]'),
    overlayTitle: panel.querySelector('[data-match-overlay-title]'),
    overlayNote: panel.querySelector('[data-match-overlay-note]'),
    results: root.querySelector('[data-result-modal]'),
    resultTitle: root.querySelector('[data-result-title]'),
    resultReason: root.querySelector('[data-result-reason]'),
    resultDuration: root.querySelector('[data-result-duration]'),
    resultBoard: root.querySelector('[data-result-board]'),
    rematch: root.querySelector('[data-result-rematch]'),
    waiting: root.querySelector('[data-result-waiting]'),
    resultLeave: root.querySelector('[data-result-leave]')
  };

  panel.querySelector('[data-match-leave]').addEventListener('click', leave);
  state.elements.rematch.addEventListener('click', rematch);
  state.elements.resultLeave.addEventListener('click', leave);

  window.addEventListener('keydown', onKeyDown);
  window.addEventListener('resize', function () {
    if (state.active) {
      sizeCanvas();
    }
  });
}
