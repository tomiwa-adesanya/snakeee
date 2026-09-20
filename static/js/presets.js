// Named presets, presented as selectable cards.
//
// three ship with the game and are read only; the player
// saves their own under any other name.
//
// A card carries the name and a summary of what the setup actually is, so
// choosing one does not require opening the rule editor to find out what it
// does. That was the main complaint about the first version of this screen:
// the only way to know what "Brawl" meant was to load it and read forty
// controls.
//
// There is no confirm() anywhere in this codebase. Deleting opens a modal that
// names the preset in the confirm button, so the destructive action is spelled
// out rather than labelled OK.

import { summarise } from './rules.js';

let elements = {};
let schema = null;
let handlers = {};
let presets = [];
let selectedName = null;
let pendingDelete = null;

export async function listPresets() {
  const response = await fetch('/api/presets', { cache: 'no-store' });
  if (!response.ok) {
    throw new Error('presets could not be loaded');
  }
  return response.json();
}

function setStatus(message, isError) {
  // There is one of these on the setups screen and one in the Customise footer.
  // Both should say the same thing.
  elements.status.forEach(function (node) {
    node.textContent = message || '';
    node.classList.toggle('is-error', Boolean(isError));
  });
}

function card(preset) {
  const node = document.createElement('div');
  node.className = 'preset-card';
  node.dataset.preset = preset.name;
  node.tabIndex = 0;
  node.setAttribute('role', 'button');

  if (preset.name === selectedName) {
    node.classList.add('is-selected');
  }

  const head = document.createElement('div');
  head.className = 'preset-card-head';

  const title = document.createElement('h3');
  title.textContent = preset.name;
  head.appendChild(title);

  if (preset.builtin) {
    const tag = document.createElement('span');
    tag.className = 'preset-tag';
    tag.textContent = 'Shipped';
    head.appendChild(tag);
  } else {
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'preset-delete';
    remove.textContent = 'Delete';
    remove.setAttribute('aria-label', 'Delete ' + preset.name);
    remove.addEventListener('click', function (event) {
      event.stopPropagation();
      askToDelete(preset.name);
    });
    head.appendChild(remove);
  }

  node.appendChild(head);

  const chips = document.createElement('div');
  chips.className = 'preset-chips';
  summarise(schema, preset.rules).forEach(function (part) {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.textContent = part;
    chips.appendChild(chip);
  });
  node.appendChild(chips);

  function choose() {
    select(preset.name);
  }

  node.addEventListener('click', choose);
  node.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      choose();
    }
  });
  node.addEventListener('dblclick', function () {
    choose();
    if (handlers.onPlay) {
      handlers.onPlay();
    }
  });

  return node;
}

export function select(name) {
  const preset = presets.find(function (row) {
    return row.name === name;
  });
  if (!preset) {
    return;
  }

  selectedName = name;

  elements.cards.querySelectorAll('.preset-card').forEach(function (node) {
    node.classList.toggle('is-selected', node.dataset.preset === name);
  });

  if (handlers.onSelect) {
    handlers.onSelect(preset);
  }
}

export function selected() {
  return presets.find(function (row) {
    return row.name === selectedName;
  }) || null;
}

export async function refresh(keepSelection) {
  let payload;
  try {
    payload = await listPresets();
  } catch (error) {
    setStatus(error.message, true);
    return null;
  }

  presets = payload.presets;

  if (payload.last_used) {
    presets = [
      { name: 'Last played', builtin: true, rules: payload.last_used, recent: true }
    ].concat(presets);
  }

  if (!keepSelection || !selected()) {
    selectedName = presets.length ? presets[0].name : null;
  }

  elements.cards.textContent = '';
  presets.forEach(function (preset) {
    elements.cards.appendChild(card(preset));
  });

  if (selectedName) {
    select(selectedName);
  }

  return payload;
}

async function savePreset() {
  const name = elements.name.value;
  if (!name.trim()) {
    setStatus('Give the setup a name first.', true);
    elements.name.focus();
    return;
  }

  const rules = handlers.collectRules ? handlers.collectRules() : {};

  try {
    const response = await fetch('/api/presets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name, rules: rules })
    });
    const payload = await response.json();

    if (!response.ok) {
      setStatus(payload.detail || 'The setup was refused.', true);
      return;
    }

    elements.name.value = '';
    selectedName = payload.name;
    await refresh(true);

    // The stored name, and a word when it is not the one that was typed.
    // Whitespace is collapsed on the way in, and a setup saved under a name the
    // player never saw is one they will go looking for and not find.
    setStatus(
      payload.name === name.trim()
        ? 'Saved as ' + payload.name + '.'
        : 'Saved as ' + payload.name + '. (The name was tidied up.)'
    );
  } catch (error) {
    setStatus(error.message, true);
  }
}

function askToDelete(name) {
  pendingDelete = name;
  elements.confirmName.textContent = name;
  elements.confirmButton.textContent = 'Delete ' + name;
  elements.confirm.hidden = false;
  elements.confirmButton.focus();
}

function closeConfirm() {
  pendingDelete = null;
  elements.confirm.hidden = true;
}

async function confirmDelete() {
  if (!pendingDelete) {
    return;
  }

  const name = pendingDelete;
  closeConfirm();

  try {
    const response = await fetch('/api/presets/' + encodeURIComponent(name), {
      method: 'DELETE'
    });
    if (!response.ok) {
      const payload = await response.json().catch(function () {
        return {};
      });
      setStatus(payload.detail || 'The setup could not be deleted.', true);
      return;
    }

    if (selectedName === name) {
      selectedName = null;
    }
    await refresh(true);
    setStatus('Deleted ' + name + '.');
  } catch (error) {
    setStatus(error.message, true);
  }
}

export function initPresets(root, options) {
  schema = options.schema;
  handlers = options;

  elements = {
    cards: root.querySelector('[data-preset-cards]'),
    name: root.querySelector('[data-preset-name]'),
    save: root.querySelector('[data-preset-save]'),
    status: Array.prototype.slice.call(
      root.querySelectorAll('[data-preset-status]')
    ),
    confirm: root.querySelector('[data-preset-confirm]'),
    confirmName: root.querySelector('[data-preset-confirm-name]'),
    confirmButton: root.querySelector('[data-preset-confirm-delete]'),
    confirmCancel: root.querySelector('[data-preset-confirm-cancel]')
  };

  if (!elements.cards) {
    return;
  }

  elements.save.addEventListener('click', savePreset);
  elements.confirmButton.addEventListener('click', confirmDelete);
  elements.confirmCancel.addEventListener('click', closeConfirm);

  elements.name.addEventListener('keydown', function (event) {
    if (event.key === 'Enter') {
      event.preventDefault();
      savePreset();
    }
  });
}
