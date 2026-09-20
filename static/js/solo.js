// Single player controller.
//
// The engine runs in Python. This module sends headings, receives snapshots,
// and renders. It contains no movement rules, no collision logic and no
// scoring. A second implementation of the game loop in JavaScript is how single
// player and multiplayer would drift apart, so there is not one.
//
// Three details matter to how the game feels, and none of them are obvious:
//
//   1. Rendering extrapolates between snapshots. Snapshots arrive 20 times a
//      second and the screen redraws 60 times a second, so drawing the reported
//      progress as-is freezes the snake for two frames out of three and then
//      jumps it forward. At slow speeds, where a move lasts 200 ms, that reads
//      as stutter.
//   2. A heading is only skipped when the engine already has it queued. Never
//      because it was refused before: whether a turn is legal depends on where
//      the snake is now, not on what was pressed earlier.
//   3. The queued heading is drawn. The engine turns only on a move boundary,
//      so at slow speeds a keypress can wait a fifth of a second. The head's
//      eyes point at the queued direction immediately, which is what makes the
//      input feel answered.

import { headingFor, isGameKey, isPauseKey, isRestartKey } from './input.js';
import {
  initMultiplayer,
  resumeMultiplayer,
  suspendMultiplayer
} from './multiplayer.js';
import { initMatch } from './match.js';
import { initPresets, refresh as refreshPresets, selected } from './presets.js';
import {
  applyValues,
  buildEditor,
  fetchSchema,
  readValues,
  showErrors
} from './rules.js';
import { lockScroll, registerScreens, showScreen } from './screens.js';
import {
  createBackground,
  drawArenaBorder,
  drawFood,
  drawItems,
  drawSnake,
  interpolateBody,
  itemStyle,
  shifted,
  themeFor,
  useThemeSet
} from './render.js';

const SNAPSHOT_HZ = 20;
const SNAPSHOT_INTERVAL = 1000 / SNAPSHOT_HZ;
const MAX_CELL = 34;
const MIN_CELL = 5;

// Never draw a body more than this far into the next cell. Without a cap, a
// late snapshot lets extrapolation put the head inside a wall it has not hit.
const MAX_PROGRESS = 0.94;

const state = {
  running: false,
  phase: 'idle',
  arena: null,
  cell: 14,
  background: null,
  snapshot: null,
  snapshotAt: 0,
  best: 0,
  pollTimer: null,
  frame: null,
  elements: {},
  schema: null,
  editor: null,
  baseRules: {},
  validateTimer: null,
  valid: true,
  observer: null
};

// -- helpers ----------------------------------------------------------------

async function postJson(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });

  const payload = await response.json().catch(function () {
    return {};
  });

  if (!response.ok) {
    const error = new Error(payload.detail || payload.error || response.statusText);
    error.code = payload.error;
    throw error;
  }

  return payload;
}

// -- rules ------------------------------------------------------------------

// The editor renders only the solo-scoped fields. baseRules carries everything
// else, so a setup saved from this screen keeps its multiplayer settings.
function currentRules() {
  if (!state.schema || !state.editor) {
    return Object.assign({}, state.baseRules);
  }
  return readValues(state.elements.editor, state.schema, state.baseRules);
}

function loadRules(rules) {
  if (!rules || !state.schema) {
    return;
  }
  state.baseRules = Object.assign({}, state.baseRules, rules);
  applyValues(state.elements.editor, state.schema, rules);
  scheduleValidation();
}

function scheduleValidation() {
  window.clearTimeout(state.validateTimer);
  state.validateTimer = window.setTimeout(validateNow, 180);
}

// Validation runs server side as the player edits, so a bad value is named
// where it was entered rather than rejected on submit. It also returns the
// tick intervals, which is what the speed line reports.
async function validateNow() {
  try {
    const response = await fetch('/api/rules/validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rules: currentRules() })
    });
    const payload = await response.json();

    showErrors(state.elements.editor, payload.errors);
    state.valid = payload.valid;
    state.elements.customiseStart.disabled = !payload.valid;

    if (payload.valid) {
      state.elements.speedFast.textContent = (60 / payload.base_interval_ticks)
        .toFixed(1);
      state.elements.speedSlow.textContent = (60 / payload.max_interval_ticks)
        .toFixed(1);
    }
  } catch (error) {
    console.error('validation failed', error);
  }
}

async function showBestFor(rules) {
  try {
    const response = await fetch('/api/rules/validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rules: rules })
    });
    const payload = await response.json();
    if (!payload.valid || !payload.fingerprint) {
      state.elements.setupBest.textContent = '0';
      return;
    }

    const history = await fetch(
      '/api/solo/history?rules=' + encodeURIComponent(payload.fingerprint),
      { cache: 'no-store' }
    );
    const body = await history.json();
    state.elements.setupBest.textContent = body.best || 0;
  } catch (error) {
    state.elements.setupBest.textContent = '0';
  }
}

// -- rendering --------------------------------------------------------------

function sizeCanvas() {
  const arena = state.arena;
  const canvas = state.elements.canvas;
  const surface = state.elements.surface;

  if (!arena || !canvas || !surface) {
    return;
  }

  const available = surface.getBoundingClientRect();

  // Nothing sensible can be computed before the browser has laid the board out.
  // Bailing out here and letting the observer below call again is better than
  // sizing the arena against a height of zero.
  if (available.height < 40 || available.width < 40) {
    return;
  }

  // Quarter-pixel steps rather than whole ones. Flooring to an integer throws
  // away up to one cell of arena in each direction, which on an 80x80 board was
  // about seventy pixels of height. Everything already draws at fractional cell
  // positions for interpolation, so this costs nothing.
  const raw = Math.min(available.width / arena.w, available.height / arena.h);
  const stepped = Math.floor(raw * 4) / 4;
  const cell = Math.max(MIN_CELL, Math.min(MAX_CELL, stepped));

  state.cell = cell;

  const ratio = window.devicePixelRatio || 1;
  const width = arena.w * cell;
  const height = arena.h * cell;

  canvas.style.width = width + 'px';
  canvas.style.height = height + 'px';
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);

  // Scale by what the backing store actually ended up being, not by the device
  // ratio, so a fractional cell size cannot leave the drawing half a pixel out.
  const context = canvas.getContext('2d');
  context.setTransform(canvas.width / width, 0, 0, canvas.height / height, 0, 0);

  state.background = createBackground(arena.w, arena.h, cell, themeFor(0));
}

// How far through its current move a snake is, right now. The snapshot value is
// already stale by the time it is drawn, so the elapsed time since it arrived
// is added back.
function progressFor(snake, now) {
  if (state.phase !== 'playing') {
    return 0;
  }

  const duration = snake.interval_ms || 100;
  const elapsed = now - state.snapshotAt;

  return Math.min(MAX_PROGRESS, snake.p + elapsed / duration);
}

function watchSurface() {
  if (state.observer || typeof window.ResizeObserver !== 'function') {
    return;
  }

  state.observer = new window.ResizeObserver(function () {
    if (state.running) {
      sizeCanvas();
    }
  });
  state.observer.observe(state.elements.surface);
}

function render(now) {
  const canvas = state.elements.canvas;
  if (!canvas || !state.arena) {
    state.frame = null;
    return;
  }

  const ctx = canvas.getContext('2d');
  const cell = state.cell;
  const width = state.arena.w * cell;
  const height = state.arena.h * cell;

  ctx.clearRect(0, 0, width, height);

  if (state.background) {
    ctx.drawImage(state.background, 0, 0, width, height);
  }

  const snapshot = state.snapshot;
  if (snapshot) {
    drawFood(ctx, snapshot.food || [], cell);
    drawItems(ctx, snapshot.items || [], cell);

    (snapshot.snakes || []).forEach(function (snake) {
      const step = interpolateBody(
        snake.b, snake.h, snake.n, progressFor(snake, now), state.arena
      );
      const options = {
        cell: cell,
        heading: snake.h,
        intent: snake.ph,
        alpha: snake.alive ? 1 : 0.45
      };

      drawSnake(ctx, step.body, snake.c, options);

      // Mid-crossing, the head is partly outside the arena. Draw the half
      // coming in on the opposite side, so a wrap is continuous rather than a
      // disappearance followed by a reappearance. The shift comes from the
      // target cell the engine reported, so it only happens on a real wrap and
      // not when the player turns instead.
      const incoming = shifted(step.body, step.shift);
      if (incoming) {
        drawSnake(ctx, incoming, snake.c, options);
      }
    });
  }

  if (state.arena.edge === 'walls') {
    drawArenaBorder(ctx, state.arena.w, state.arena.h, cell);
  }

  state.frame = window.requestAnimationFrame(render);
}

// -- HUD --------------------------------------------------------------------

function updateHud() {
  const snapshot = state.snapshot;
  const snake = snapshot && snapshot.snakes && snapshot.snakes[0];

  state.elements.score.textContent = snake ? snake.score : 0;
  state.elements.length.textContent = snake ? snake.len : 0;
  state.elements.speed.textContent = snake ? snake.mps.toFixed(1) : 0;
  state.elements.best.textContent = state.best;

  renderEffects(snake);

  const paused = state.phase === 'paused';
  state.elements.pauseButton.textContent = paused ? 'Resume' : 'Pause';
  state.elements.hud.classList.toggle('is-paused', paused);
}

// What is acting on the snake and how long is left of it. An effect whose end
// the player cannot see reads as a bug rather than as a mechanic.
function renderEffects(snake) {
  const holder = state.elements.effects;
  if (!holder) {
    return;
  }

  const active = snake && snake.fx ? snake.fx : {};
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

function closeGameOver() {
  if (state.elements.modal.hidden) {
    return false;
  }
  quitMatch();
  return true;
}

function showGameOver(snapshot) {
  const result = snapshot.result || {};
  // A fault outranks a death cause. If the engine stopped because it raised
  // there was no death, and 'the arena filled up' would be a guess presented as
  // an explanation.
  const reason = snapshot.fault
    ? 'The game stopped because of an error. The log file has the details.'
    : ({
        wall: 'You hit the wall.',
        self: 'You ran into yourself.'
      }[snapshot.death] || 'The arena filled up.');

  state.elements.overReason.textContent = reason;
  state.elements.overScore.textContent = result.score === undefined ? 0 : result.score;
  state.elements.overLength.textContent = result.length || 0;
  state.elements.overDuration.textContent = (result.duration_seconds || 0) + 's';
  state.elements.overBest.textContent = snapshot.best || 0;
  state.elements.overNewBest.hidden = !snapshot.is_new_best;

  state.elements.modal.hidden = false;
  lockScroll(true);
  state.elements.playAgain.focus();
}

// -- transport --------------------------------------------------------------

function applySnapshot(snapshot) {
  // Requests can overtake each other. A snapshot from an earlier tick than the
  // one already drawn is stale and applying it rewinds the game visually.
  if (
    state.snapshot &&
    typeof snapshot.t === 'number' &&
    typeof state.snapshot.t === 'number' &&
    snapshot.t < state.snapshot.t &&
    snapshot.phase === state.phase
  ) {
    return;
  }

  state.snapshot = snapshot;
  state.snapshotAt = window.performance.now();
  state.phase = snapshot.phase;

  if (typeof snapshot.best === 'number') {
    state.best = snapshot.best;
  }

  updateHud();

  if (snapshot.phase === 'over') {
    stopPolling();

    // Only the first observation opens the dialog. The server attaches the same
    // result to every snapshot of a finished match, but reopening would reset
    // focus and could redraw with whatever landed last.
    if (state.elements.modal.hidden) {
      showGameOver(snapshot);
    }
  }
}

async function poll() {
  try {
    const response = await fetch('/api/solo/state', { cache: 'no-store' });
    if (response.ok) {
      applySnapshot(await response.json());
    }
  } catch (error) {
    console.error('snapshot poll failed', error);
  }
}

function startPolling() {
  stopPolling();
  state.pollTimer = window.setInterval(poll, SNAPSHOT_INTERVAL);
}

function stopPolling() {
  if (state.pollTimer) {
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

// -- lifecycle --------------------------------------------------------------

async function startMatch() {
  state.elements.modal.hidden = true;
  lockScroll(false);

  let keyframe;
  try {
    keyframe = await postJson('/api/solo/start', { rules: currentRules() });
  } catch (error) {
    state.elements.soloError.textContent = error.message;
    state.elements.soloError.hidden = false;
    showScreen('solo');
    return;
  }

  state.elements.soloError.hidden = true;

  // Before anything is drawn, and before the background is built: the theme
  // decides every colour on the board, so choosing it after the first frame
  // would show one frame of the last theme.
  useThemeSet(currentRules().theme_set);
  state.background = null;

  state.arena = keyframe.arena;
  state.snapshot = keyframe.state;
  state.snapshotAt = window.performance.now();
  state.phase = keyframe.state.phase;
  state.running = true;

  showScreen('board');

  watchSurface();

  // Once now, and again after the browser has laid the board out. The first
  // call is usually correct; the second covers the case where it was not.
  sizeCanvas();
  window.requestAnimationFrame(sizeCanvas);

  updateHud();
  startPolling();

  if (!state.frame) {
    state.frame = window.requestAnimationFrame(render);
  }

  state.elements.canvas.focus();
}

async function togglePause() {
  if (!state.running || state.phase === 'over') {
    return;
  }
  try {
    const result = await postJson('/api/solo/pause');
    state.phase = result.phase;
    state.snapshotAt = window.performance.now();
    updateHud();
  } catch (error) {
    console.error('pause failed', error);
  }
}

async function quitMatch() {
  await postJson('/api/solo/stop').catch(function () {});
  stopPolling();

  state.elements.modal.hidden = true;
  lockScroll(false);
  state.running = false;
  state.phase = 'idle';

  showScreen('solo');
  refreshPresets(true);
}

// The engine holds one queued heading and only turns on a move boundary, so
// resending the heading it already holds is pointless. Anything else is sent,
// including a direction the engine refused a moment ago: whether a turn is
// legal depends on where the snake is now, not on what happened earlier.
function serverHasQueued(heading) {
  const snake = state.snapshot && state.snapshot.snakes && state.snapshot.snakes[0];
  if (!snake || !snake.ph) {
    return false;
  }

  const vectors = {
    up: [0, -1],
    down: [0, 1],
    left: [-1, 0],
    right: [1, 0]
  };
  const wanted = vectors[heading];

  return Boolean(wanted) && snake.ph[0] === wanted[0] && snake.ph[1] === wanted[1];
}

async function sendHeading(heading) {
  if (!state.running || state.phase !== 'playing') {
    return;
  }
  if (serverHasQueued(heading)) {
    return;
  }

  try {
    const reply = await postJson('/api/solo/input', { heading: heading });

    // The reply carries the state the turn produced, including the cell the
    // head is now moving into. Applying it here is what removes the flash at a
    // wrapping edge: without it the renderer keeps sliding toward the old
    // target, which is off the far side of the arena, until the next poll.
    if (reply.state) {
      applySnapshot(reply.state);
    }
  } catch (error) {
    // A refused reversal is normal play, not a failure worth reporting, and
    // nothing about it is remembered.
    if (error.code !== 'invalid_heading' && error.code !== 'no_match') {
      console.error('input failed', error);
    }
  }
}

function onKeyDown(event) {
  // Escape closes the game over dialog first. Otherwise it is the pause key,
  // and after a death there is nothing to pause.
  if (event.key === 'Escape' && !state.elements.modal.hidden) {
    event.preventDefault();
    closeGameOver();
    return;
  }

  if (!state.running || !isGameKey(event)) {
    return;
  }

  event.preventDefault();

  if (event.repeat) {
    return;
  }

  if (isPauseKey(event)) {
    togglePause();
    return;
  }

  if (isRestartKey(event)) {
    startMatch();
    return;
  }

  const heading = headingFor(event);
  if (heading) {
    sendHeading(heading);
  }
}

// -- setup ------------------------------------------------------------------

async function buildRuleEditor() {
  let schema;
  try {
    schema = await fetchSchema();
  } catch (error) {
    state.elements.soloError.textContent = error.message;
    state.elements.soloError.hidden = false;
    return;
  }

  state.schema = schema;
  state.baseRules = Object.assign({}, schema.defaults, schema.last_used || {});

  state.editor = buildEditor(
    state.elements.editor, schema, state.baseRules, 'solo'
  );
  state.editor.setShowInactive(false);
  state.elements.editor.addEventListener('input', scheduleValidation);

  state.elements.showInactive.addEventListener('change', function (event) {
    state.editor.setShowInactive(event.target.checked);
  });

  initPresets(document, {
    schema: schema,
    onSelect: function (preset) {
      loadRules(preset.rules);
      showBestFor(preset.rules);
    },
    onPlay: startMatch,
    collectRules: currentRules
  });

  await refreshPresets();
  validateNow();

  initMultiplayer(document, {
    schema: schema,
    collectRules: currentRules,
    onLeave: function () {
      showScreen('multi');
    }
  });
}

export function initSolo(root) {
  const panel = root.querySelector('[data-panel="play"]');
  if (!panel) {
    return;
  }

  registerScreens(panel);
  initMatch(root);

  state.elements = {
    editor: panel.querySelector('[data-rules-editor]'),
    showInactive: panel.querySelector('[data-show-inactive]'),
    soloStart: panel.querySelector('[data-solo-start]'),
    customiseStart: panel.querySelector('[data-customise-start]'),
    speedFast: panel.querySelector('[data-speed-fast]'),
    speedSlow: panel.querySelector('[data-speed-slow]'),
    setupBest: panel.querySelector('[data-setup-best]'),
    soloError: panel.querySelector('[data-solo-error]'),
    surface: panel.querySelector('.board-surface'),
    canvas: panel.querySelector('[data-solo-canvas]'),
    hud: panel.querySelector('[data-solo-hud]'),
    score: panel.querySelector('[data-hud-score]'),
    length: panel.querySelector('[data-hud-length]'),
    speed: panel.querySelector('[data-hud-speed]'),
    best: panel.querySelector('[data-hud-best]'),
    effects: panel.querySelector('[data-solo-effects]'),
    pauseButton: panel.querySelector('[data-solo-pause]'),
    modal: root.querySelector('[data-over-modal]'),
    overReason: root.querySelector('[data-over-reason]'),
    overScore: root.querySelector('[data-over-score]'),
    overLength: root.querySelector('[data-over-length]'),
    overDuration: root.querySelector('[data-over-duration]'),
    overBest: root.querySelector('[data-over-best]'),
    overNewBest: root.querySelector('[data-over-new-best]'),
    playAgain: root.querySelector('[data-over-again]'),
    overClose: root.querySelector('[data-over-close]'),
    overQuit: root.querySelector('[data-over-quit]')
  };

  panel.querySelector('[data-mode-solo]').addEventListener('click', function () {
    showScreen('solo');
  });
  panel.querySelector('[data-back-modes]').addEventListener('click', function () {
    showScreen('modes');
  });
  panel.querySelector('[data-back-solo]').addEventListener('click', function () {
    showScreen('solo');
    const preset = selected();
    if (preset) {
      showBestFor(currentRules());
    }
  });
  panel.querySelector('[data-open-customise]').addEventListener('click', function () {
    showScreen('customise');
    validateNow();
  });

  state.elements.soloStart.addEventListener('click', startMatch);
  state.elements.customiseStart.addEventListener('click', startMatch);
  panel.querySelector('[data-solo-quit]').addEventListener('click', quitMatch);
  state.elements.pauseButton.addEventListener('click', togglePause);
  state.elements.playAgain.addEventListener('click', startMatch);
  state.elements.overQuit.addEventListener('click', quitMatch);
  state.elements.overClose.addEventListener('click', closeGameOver);

  panel.querySelector('[data-mode-multi]').addEventListener('click', function () {
    showScreen('multi');
    resumeMultiplayer();
  });
  panel.querySelector('[data-back-modes-multi]').addEventListener('click', function () {
    suspendMultiplayer();
    showScreen('modes');
  });

  window.addEventListener('keydown', onKeyDown);
  window.addEventListener('resize', function () {
    if (state.running) {
      sizeCanvas();
    }
  });

  showScreen('modes');
  buildRuleEditor();
}

// Called when the player navigates away mid-match. Leaving a tick thread
// running behind a hidden panel is how a game ends up with two engines.
export function suspendSolo() {
  if (state.running && state.phase === 'playing') {
    togglePause();
  }
}
