// Choosing your snake's colour.
//
// One picker, used in Settings and in the single player setup, writing to one
// place: the profile. Your colour is a fact about you rather than about a
// match, so a solo game and a multiplayer room show the same snake and there is
// no second copy to disagree with the first.
//
// The feedback is the point of this module. A ring around the chosen swatch is
// easy to miss against twelve saturated colours, and a line of small grey text
// saying the change was saved is easy to miss anywhere. So the answer to "which
// one did I pick" is a drawing of the snake itself, in that colour, next to the
// swatches, redrawn the moment you click. It is the same drawing code the arena
// uses, so what you see here is what you play as.

import { PLAYER_COLOURS, drawSnake } from './render.js';

const PREVIEW_CELL = 18;
const PREVIEW_LENGTH = 5;

// Every picker on the page, so that choosing a colour in one place updates the
// others rather than leaving two screens disagreeing about what you picked.
const pickers = [];

let current = null;
let saveTimer = null;

function drawPreview(canvas, colour) {
  const ratio = window.devicePixelRatio || 1;
  const width = PREVIEW_CELL * PREVIEW_LENGTH;
  const height = PREVIEW_CELL;

  canvas.style.width = width + 'px';
  canvas.style.height = height + 'px';
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);

  const ctx = canvas.getContext('2d');
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  // Head first, as the arena stores it, so the taper runs the right way and the
  // eyes face the direction of travel.
  const body = [];
  for (let i = 0; i < PREVIEW_LENGTH; i += 1) {
    body.push({ x: PREVIEW_LENGTH - 1 - i, y: 0 });
  }

  drawSnake(ctx, body, colour, {
    cell: PREVIEW_CELL,
    heading: [1, 0],
    headGlow: 10
  });
}

function paint() {
  pickers.forEach(function (picker) {
    picker.swatches.querySelectorAll('[data-colour]').forEach(function (button) {
      const mine = button.dataset.colour === current;
      button.classList.toggle('is-selected', mine);
      button.setAttribute('aria-pressed', mine ? 'true' : 'false');
    });

    if (picker.preview) {
      drawPreview(picker.preview, current);
    }
  });
}

function report(message, failed) {
  pickers.forEach(function (picker) {
    if (!picker.status) {
      return;
    }
    picker.status.textContent = message;
    picker.status.classList.toggle('is-error', Boolean(failed));
  });
}

function save() {
  window.clearTimeout(saveTimer);

  // Said before the request rather than after it. The old picker only spoke
  // once the answer came back, so a click looked like nothing had happened
  // until it did.
  report('Saving...', false);

  saveTimer = window.setTimeout(async function () {
    try {
      const response = await fetch('/api/profile', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ colour: current })
      });
      if (!response.ok) {
        throw new Error('the server refused the change');
      }
      report('Colour saved. This is your snake in every game.', false);
    } catch (error) {
      report('Not saved: ' + error.message, true);
    }
  }, 250);
}

export function selectedColour() {
  return current;
}

export function setColour(colour, andSave) {
  if (PLAYER_COLOURS.indexOf(colour) === -1) {
    return;
  }
  current = colour;
  paint();
  if (andSave) {
    save();
  }
}

// container is anything; the picker fills it. status is optional and is shared
// by every picker, so a failure is reported wherever you are looking.
export function createColourPicker(container, status) {
  container.textContent = '';

  const swatches = document.createElement('div');
  swatches.className = 'swatches';

  PLAYER_COLOURS.forEach(function (colour) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'swatch';
    button.dataset.colour = colour;
    button.style.background = colour;
    button.setAttribute('aria-label', 'Snake colour ' + colour);
    button.addEventListener('click', function () {
      setColour(colour, true);
    });
    swatches.appendChild(button);
  });

  const preview = document.createElement('div');
  preview.className = 'colour-preview';

  const canvas = document.createElement('canvas');
  canvas.setAttribute('aria-hidden', 'true');

  const caption = document.createElement('span');
  caption.className = 'colour-preview-caption';
  caption.textContent = 'Your snake';

  preview.appendChild(canvas);
  preview.appendChild(caption);

  container.appendChild(swatches);
  container.appendChild(preview);

  pickers.push({ swatches: swatches, preview: canvas, status: status || null });

  if (current) {
    paint();
  }

  return { swatches: swatches, preview: canvas };
}

export async function loadColour() {
  try {
    const response = await fetch('/api/profile', { cache: 'no-store' });
    if (response.ok) {
      const profile = await response.json();
      current = profile.colour;
    }
  } catch (error) {
    console.error('could not load the colour', error);
  }

  if (!current) {
    current = PLAYER_COLOURS[2];
  }

  paint();
  return current;
}
