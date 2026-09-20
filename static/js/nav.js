// Floating bottom navigation.
//
// Four items, icon over label, active item carries an
// accent pill. It hides entirely during a match, through setNavVisible, so that
// the match code has something to call rather than reaching into the DOM.

const SECTIONS = ['home', 'play', 'settings', 'about'];

let currentSection = 'home';
let navElement = null;
let listeners = [];

export function initNav(root) {
  navElement = root.querySelector('[data-nav]');

  if (!navElement) {
    return;
  }

  navElement.addEventListener('click', function (event) {
    const button = event.target.closest('[data-section]');
    if (!button) {
      return;
    }
    show(button.getAttribute('data-section'));
  });

  navElement.addEventListener('keydown', function (event) {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') {
      return;
    }
    const step = event.key === 'ArrowRight' ? 1 : -1;
    const index = (SECTIONS.indexOf(currentSection) + step + SECTIONS.length) % SECTIONS.length;
    show(SECTIONS[index]);
    const next = navElement.querySelector('[data-section="' + SECTIONS[index] + '"]');
    if (next) {
      next.focus();
    }
    event.preventDefault();
  });

  show(currentSection);
}

export function show(section) {
  if (SECTIONS.indexOf(section) === -1) {
    return;
  }

  currentSection = section;

  document.querySelectorAll('[data-panel]').forEach(function (panel) {
    const active = panel.getAttribute('data-panel') === section;
    panel.classList.toggle('is-active', active);
    panel.hidden = !active;
  });

  if (navElement) {
    navElement.querySelectorAll('[data-section]').forEach(function (button) {
      const active = button.getAttribute('data-section') === section;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-current', active ? 'page' : 'false');
    });
  }

  listeners.forEach(function (listener) {
    listener(section);
  });
}

export function onSectionChange(listener) {
  listeners.push(listener);
}

// Called when a match starts and ends. Nothing floats over the arena except the
// HUD.
export function setNavVisible(visible) {
  if (!navElement) {
    return;
  }
  navElement.classList.toggle('is-hidden', !visible);
}

export function currentlyShowing() {
  return currentSection;
}
