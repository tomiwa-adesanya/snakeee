// Which screen inside the Play panel is showing.
//
// Extracted from the solo controller when the shared match arrived and needed
// the same switch. Two modules each toggling the same nodes is how two of them
// end up visible at once, which happened once already when the board was
// handled separately from the rest.

import { setNavVisible } from './nav.js';

export const SCREENS = ['modes', 'solo', 'customise', 'multi', 'board', 'match'];

// The screens on which a match is being played. The nav is hidden and the page
// is not allowed to scroll, or the arena slides around under the player.
const PLAYING = ['board', 'match'];

let nodes = {};

export function registerScreens(panel) {
  nodes = {};
  SCREENS.forEach(function (name) {
    nodes[name] = panel.querySelector('[data-screen="' + name + '"]');
  });
  return nodes;
}

export function screenNode(name) {
  return nodes[name] || null;
}

export function showScreen(name) {
  SCREENS.forEach(function (screen) {
    const node = nodes[screen];
    if (node) {
      node.hidden = screen !== name;
    }
  });

  const playing = PLAYING.indexOf(name) !== -1;

  setNavVisible(!playing);

  const shell = document.querySelector('[data-shell]');
  if (shell) {
    shell.classList.toggle('is-playing', playing);
  }
}

export function lockScroll(locked) {
  document.body.classList.toggle('is-locked', locked);
}
