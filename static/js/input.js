// Keyboard input.
//
// Arrow keys and WASD both play.
//
// The mapping lives in its own module rather than inside the game controller, so
// that key remapping and effects that scramble the controls can be applied here
// without the rest of the frontend knowing about them.

const KEY_MAP = {
  ArrowUp: 'up',
  ArrowDown: 'down',
  ArrowLeft: 'left',
  ArrowRight: 'right',
  KeyW: 'up',
  KeyS: 'down',
  KeyA: 'left',
  KeyD: 'right'
};

// Set while an effect that reverses the controls is active. Inverting here means
// the engine never has to know about it, and nothing else has to check.
let inverted = false;

const OPPOSITE = { up: 'down', down: 'up', left: 'right', right: 'left' };

export function setInverted(value) {
  inverted = Boolean(value);
}

export function headingFor(event) {
  const heading = KEY_MAP[event.code];
  if (!heading) {
    return null;
  }
  return inverted ? OPPOSITE[heading] : heading;
}

export function isPauseKey(event) {
  return event.code === 'KeyP' || event.code === 'Escape';
}

export function isRestartKey(event) {
  return event.code === 'KeyR';
}

// True for the keys the game consumes, so the caller knows when to call
// preventDefault. Arrow keys scroll the page otherwise, which moves the board
// under the player mid-match.
export function isGameKey(event) {
  return Boolean(KEY_MAP[event.code]) || isPauseKey(event) || isRestartKey(event);
}
