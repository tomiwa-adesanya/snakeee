// Boot sequence for the single page shell.
//
// Nothing in this file, or in any other frontend file, contains the game's name
// as a literal. It is fetched from /api/branding, so utils/branding.py stays the
// only place the name is written down.

import { createColourPicker, loadColour } from './colours.js';
import { initNav, onSectionChange, show } from './nav.js';
import { initSolo, suspendSolo } from './solo.js';
import { runSplash } from './splash.js';

const FALLBACK_TITLE = 'LAN Snake';

async function loadBranding() {
  try {
    const response = await fetch('/api/branding', { cache: 'no-store' });
    if (!response.ok) {
      throw new Error('branding endpoint returned ' + response.status);
    }
    return await response.json();
  } catch (error) {
    console.error('could not load branding', error);
    return { app_name: FALLBACK_TITLE, tagline: '', app_version: '' };
  }
}

function applyBranding(info) {
  document.title = info.app_name;

  document.querySelectorAll('[data-app-name]').forEach(function (node) {
    node.textContent = info.app_name;
  });
  document.querySelectorAll('[data-tagline]').forEach(function (node) {
    node.textContent = info.tagline || '';
  });
  document.querySelectorAll('[data-app-version]').forEach(function (node) {
    node.textContent = info.app_version || '';
  });
}

function initFullscreenToggle() {
  // F11 is handled in the frontend.
  window.addEventListener('keydown', function (event) {
    if (event.key !== 'F11') {
      return;
    }
    event.preventDefault();

    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else if (document.documentElement.requestFullscreen) {
      document.documentElement.requestFullscreen().catch(function (error) {
        console.warn('fullscreen refused', error);
      });
    }
  });
}

async function waitForFonts() {
  // The splash rasterises the title with the UI face. Without this the first
  // launch traces the fallback font instead.
  if (!document.fonts || !document.fonts.ready) {
    return;
  }
  try {
    await document.fonts.load('700 48px "Space Grotesk"');
    await document.fonts.ready;
  } catch (error) {
    console.warn('font loading did not complete', error);
  }
}

// -- profile ----------------------------------------------------------------

let saveTimer = null;

async function saveProfile(patch, status) {
  window.clearTimeout(saveTimer);

  saveTimer = window.setTimeout(async function () {
    status.classList.remove('is-error');
    try {
      const response = await fetch('/api/profile', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch)
      });
      if (!response.ok) {
        throw new Error('the server refused the change');
      }
      status.textContent = 'Saved on this machine.';
    } catch (error) {
      status.textContent = 'Not saved: ' + error.message;
      status.classList.add('is-error');
    }
  }, 300);
}

async function initProfile(root) {
  const form = root.querySelector('[data-profile-form]');
  const status = root.querySelector('[data-profile-status]');

  if (!form) {
    return;
  }

  // Was an onsubmit attribute on the form until the Content-Security-Policy
  // arrived. A policy without unsafe-inline blocks inline handlers, and the
  // failure is not loud: the attribute stops running, the form submits, and the
  // whole page reloads on Enter. That is the value of the policy rather than a
  // cost of it, since it will do the same to the next one somebody adds.
  form.addEventListener('submit', function (event) {
    event.preventDefault();
  });

  const username = form.querySelector('[name="username"]');

  // Both pickers are registered before the colour is fetched, so the one answer
  // paints both. Building one, fetching, then building the other would leave
  // whichever came second unpainted until the next click.
  root.querySelectorAll('[data-colour-picker]').forEach(function (node) {
    createColourPicker(node, status);
  });

  const solo = root.querySelector('[data-solo-colour]');
  if (solo) {
    createColourPicker(solo, root.querySelector('[data-solo-colour-status]'));
  }

  await loadColour();

  try {
    const response = await fetch('/api/profile', { cache: 'no-store' });
    if (response.ok) {
      username.value = (await response.json()).username;
    }
  } catch (error) {
    console.error('could not load the profile', error);
  }

  username.addEventListener('input', function () {
    saveProfile({ username: username.value }, status);
  });
}

function initHomeActions(root) {
  const play = root.querySelector('[data-home-play]');
  const settings = root.querySelector('[data-home-settings]');

  if (play) {
    play.addEventListener('click', function () {
      show('play');
    });
  }
  if (settings) {
    settings.addEventListener('click', function () {
      show('settings');
    });
  }
}

async function boot() {
  const shell = document.querySelector('[data-shell]');
  const overlay = document.querySelector('[data-splash]');
  const canvas = overlay ? overlay.querySelector('canvas') : null;

  initNav(document);
  initFullscreenToggle();
  initSolo(document);
  initProfile(document);
  initHomeActions(document);

  // Navigating away from a running match pauses it rather than leaving a tick
  // thread running behind a hidden panel.
  onSectionChange(function (section) {
    if (section !== 'play') {
      suspendSolo();
    }
  });

  const info = await loadBranding();
  applyBranding(info);

  await waitForFonts();

  if (!overlay || !canvas) {
    if (shell) {
      shell.classList.add('is-ready');
    }
    return;
  }

  runSplash(overlay, canvas, info.app_name.toUpperCase(), function () {
    if (shell) {
      shell.classList.add('is-ready');
    }
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
