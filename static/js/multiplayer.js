// Multiplayer: hosting a room, joining one, and the lobby.
//
// This screen gets two instances into a shared lobby with agreed rules and
// hands over to match.js when the room leaves the lobby. The handover is driven
// by the room's own phase rather than by the button that was clicked, so a
// player who did not click anything, because somebody else's host started the
// match, arrives on the board the same way.
//
// The room state is polled rather than pushed to the browser. The host and the
// joined client both keep authoritative state in Python, and the lobby changes
// a few times a minute rather than twenty times a second, so a two-second poll
// is the whole requirement.
//
// The room list is polled the same way and for the same reason. No UDP happens
// here: a listener thread in Python collects beacons continuously, and this
// only reads whatever it has heard, so the poll interval affects how quickly
// the list updates and nothing about how rooms are found.

import { enterMatch, exitMatch, matchIsActive, setWatching } from './match.js';
import { listPresets } from './presets.js';
import {
  applyValues,
  buildEditor,
  humanise,
  readValues,
  showErrors,
  summarise
} from './rules.js';

const POLL_INTERVAL = 2000;
const DISCOVER_INTERVAL = 2000;

const state = {
  elements: {},
  mode: 'idle',
  snapshot: null,
  pollTimer: null,
  discoverTimer: null,
  discoverAnswered: false,
  active: false,
  lastTarget: '',
  lastSpectate: false,
  pendingColour: null,
  rulesFor: null,
  onLeave: null,
  schema: null
};

// -- helpers ----------------------------------------------------------------

async function call(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });

  const payload = await response.json().catch(function () {
    return {};
  });

  return { ok: response.ok, status: response.status, payload: payload };
}

function rulesNotice(message) {
  const node = state.elements.rulesNotice;
  node.textContent = message || '';
  node.hidden = !message;
}

function setNotice(message, kind) {
  const node = state.elements.notice;
  node.textContent = message || '';
  node.hidden = !message;
  node.className = 'notice ' + (kind === 'error' ? 'notice-error' : 'notice-info');
}

function showPanel(name) {
  ['choice', 'join', 'lobby', 'rules'].forEach(function (panel) {
    state.elements.panels[panel].hidden = panel !== name;
  });

  // The list is only polled while it is on screen. The listener in Python
  // keeps running either way, so nothing is missed by not asking.
  if (name === 'choice') {
    startDiscovery();
  } else {
    stopDiscovery();
  }
}

// -- lobby rendering --------------------------------------------------------

function renderPlayers(room, myId, amHost) {
  const list = state.elements.players;
  list.textContent = '';

  room.players.forEach(function (player) {
    const row = document.createElement('div');
    row.className = 'player';

    const dot = document.createElement('span');
    dot.className = 'player-dot';
    dot.style.background = player.colour;
    row.appendChild(dot);

    const name = document.createElement('span');
    name.className = 'player-name';
    name.textContent = player.username;
    if (player.id === myId) {
      name.textContent += ' (you)';
    }
    row.appendChild(name);

    if (player.host) {
      const tag = document.createElement('span');
      tag.className = 'player-tag';
      tag.textContent = 'Host';
      row.appendChild(tag);
    }

    if (typeof player.latency_ms === 'number') {
      const latency = document.createElement('span');
      latency.className = 'player-latency mono';
      latency.textContent = player.latency_ms + 'ms';

      // A median far above the best means the link is going idle between
      // packets rather than being slow, and those are different problems with
      // different fixes. Showing both is the difference between guessing and
      // knowing.
      if (typeof player.latency_best_ms === 'number'
          && player.latency_ms - player.latency_best_ms >= 20) {
        latency.textContent += ' (best ' + player.latency_best_ms + ')';
      }

      row.appendChild(latency);
    }

    const ready = document.createElement('span');
    ready.className = 'player-ready' + (player.ready ? ' is-ready' : '');
    ready.textContent = player.ready ? 'Ready' : 'Not ready';
    row.appendChild(ready);

    if (amHost && !player.host) {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'preset-delete';
      remove.textContent = 'Remove';
      remove.addEventListener('click', function () {
        call('/api/room/kick', { player: player.id });
      });
      row.appendChild(remove);
    }

    list.appendChild(row);
  });

  const free = room.player_cap - room.players.length;
  state.elements.capacity.textContent = room.players.length + ' of ' +
    room.player_cap + ' seats taken' + (free > 0 ? ', ' + free + ' free' : '');
}

// Who is watching, kept apart from who is playing.
//
// A separate list rather than extra rows among the players, because everything
// the lobby says about starting, the count, the cap, who is not ready, counts
// the players and must go on counting only them. Two lists cannot be added up
// by accident; one list with a flag on some rows eventually is.
function renderSpectators(room, myId, amHost) {
  const holder = state.elements.watchers;
  const list = state.elements.spectators;
  const note = state.elements.spectatorNote;
  const watchers = room.spectators || [];

  if (!watchers.length) {
    holder.hidden = true;
    return;
  }

  holder.hidden = false;
  list.textContent = '';

  const names = {};
  room.players.forEach(function (player) {
    names[player.id] = player.username;
  });

  watchers.forEach(function (watcher) {
    const row = document.createElement('div');
    row.className = 'player';

    const name = document.createElement('span');
    name.className = 'player-name';
    name.textContent = watcher.username + (watcher.id === myId ? ' (you)' : '');
    row.appendChild(name);

    const tag = document.createElement('span');
    tag.className = 'spectator-tag';
    tag.textContent = names[watcher.watching]
      ? 'following ' + names[watcher.watching]
      : 'watching';
    row.appendChild(tag);

    if (amHost) {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'preset-delete';
      remove.textContent = 'Remove';
      remove.addEventListener('click', function () {
        call('/api/room/kick', { player: watcher.id });
      });
      row.appendChild(remove);
    }

    list.appendChild(row);
  });

  note.textContent = watchers.length + ' of ' + (room.spectator_cap || 0)
    + ' watching. They take no seat and do not hold up the start.';
}

function renderRules(rules) {
  const list = state.elements.rules;
  list.textContent = '';

  if (!rules || !state.schema) {
    return;
  }

  summarise(state.schema, rules).forEach(function (part) {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.textContent = part;
    list.appendChild(chip);
  });

  const cap = document.createElement('span');
  cap.className = 'chip';
  cap.textContent = 'up to ' + rules.player_cap + ' players';
  list.appendChild(cap);

  const win = document.createElement('span');
  win.className = 'chip';
  win.textContent = humanise(rules.win_condition).toLowerCase();
  list.appendChild(win);
}

function renderLobby(snapshot) {
  const room = snapshot.room;
  if (!room) {
    return;
  }

  const amHost = snapshot.mode === 'hosting';
  const watching = Boolean(snapshot.spectator);
  const me = room.players.find(function (player) {
    return player.id === snapshot.player_id;
  });

  state.elements.lobbyTitle.textContent = amHost
    ? 'Your room'
    : (watching ? 'Watching' : 'Room');
  state.elements.hostOnly.hidden = !amHost;

  if (amHost) {
    const addresses = snapshot.addresses || [];

    state.elements.codeBlock.hidden = !snapshot.code_pretty;
    state.elements.code.textContent = snapshot.code_pretty || '';

    // Beside the generated code, never instead of it. A chosen code only
    // works for somebody who can hear this room broadcasting, so it is the
    // convenience and the generated one is the guarantee.
    const chosen = snapshot.chosen_code || '';
    state.elements.chosenCode.hidden = !chosen;
    state.elements.chosenCode.textContent = chosen
      ? 'Players on this network can also type ' + chosen + '.'
      : '';

    state.elements.advanced.hidden = addresses.length === 0;
    state.elements.addresses.textContent = addresses.length
      ? addresses.join('    ')
      : '';

    if (!snapshot.code_pretty) {
      state.elements.hostOnly.textContent = 'No network interface was found, so '
        + 'there is no code to share. Others will not be able to reach you.';
    }

    // Visibility is a rule, so the host can change it from the lobby and this
    // has to follow it rather than being set once when the room opened.
    state.elements.visibility.hidden = false;
    state.elements.visibility.textContent = snapshot.visible
      ? 'This room is public, so players on this network see it in their room '
        + 'list without needing the code.'
      : 'This room is private. It does not appear in anyone else\'s room list, '
        + 'so the code is the only way in.';

    state.elements.connected.hidden = true;
  } else {
    state.elements.codeBlock.hidden = true;
    state.elements.chosenCode.hidden = true;
    state.elements.advanced.hidden = true;
    state.elements.visibility.hidden = true;
    state.elements.connected.hidden = false;
    state.elements.connected.textContent = 'Connected to ' + (snapshot.address || '');
  }

  renderPlayers(room, snapshot.player_id, amHost);
  renderSpectators(room, snapshot.player_id, amHost);
  renderRules(snapshot.rules);

  // Only the host can change them, and the endpoint refuses anybody else, so
  // offering the button to a guest would be offering a refusal.
  state.elements.editRules.hidden = !amHost;

  // A spectator is not counted by the start check, so a ready button on one
  // would be a control that changes nothing anybody is waiting on.
  state.elements.readyButton.hidden = watching;
  state.elements.readyButton.textContent = me && me.ready
    ? 'Not ready'
    : "I'm ready";

  state.elements.startNote.textContent = watching
    ? 'You are watching. The board opens when the host starts the match.'
    : (room.can_start ? 'Everyone is ready.' : (room.start_blocked_by || ''));

  // Only the host has a start button, and it is disabled rather than hidden
  // when the room is not ready, so the reason beside it has something to
  // explain.
  state.elements.startButton.hidden = !amHost;
  state.elements.startButton.disabled = !room.can_start;

  state.elements.leaveButton.textContent = amHost
    ? 'Close room'
    : (watching ? 'Stop watching' : 'Leave room');
}

// -- the room list ----------------------------------------------------------

function roomBlockedReason(room) {
  if (!room.compatible) {
    return 'Different version';
  }
  if (room.players >= room.cap) {
    return 'Full';
  }
  if (!room.joinable) {
    return 'In progress';
  }
  return null;
}

function renderRoom(room) {
  const row = document.createElement('div');
  row.className = 'room';

  const name = document.createElement('span');
  name.className = 'room-name';
  name.textContent = room.name;
  row.appendChild(name);

  if (room.host) {
    const host = document.createElement('span');
    host.className = 'room-host';
    host.textContent = room.host;
    row.appendChild(host);
  }

  const count = document.createElement('span');
  count.className = 'room-count mono';
  count.textContent = room.players + '/' + room.cap;
  row.appendChild(count);

  const blocked = roomBlockedReason(room);

  if (blocked) {
    const status = document.createElement('span');
    status.className = 'room-status';
    status.textContent = blocked;
    row.appendChild(status);
  } else {
    const join = document.createElement('button');
    join.type = 'button';
    join.className = 'button button-primary room-join';
    join.textContent = 'Join';
    join.addEventListener('click', function () {
      attemptJoin(room.target, null, false);
    });
    row.appendChild(join);
  }

  // Offered whether or not the room can be played in, and withheld only for a
  // room running a different release. A full room in the middle of a match is
  // unjoinable and is exactly the one somebody wants to watch, so the reason
  // Join is absent is not a reason to hide this.
  if (room.spectators && room.compatible) {
    const watch = document.createElement('button');
    watch.type = 'button';
    watch.className = 'button room-watch';
    watch.textContent = 'Watch';
    watch.addEventListener('click', function () {
      attemptJoin(room.target, null, true);
    });
    row.appendChild(watch);
  }

  return row;
}

function renderRooms(payload) {
  const list = state.elements.rooms;
  const note = state.elements.roomsNote;
  const rooms = payload.rooms || [];

  list.textContent = '';
  rooms.forEach(function (room) {
    list.appendChild(renderRoom(room));
  });

  if (!payload.listening) {
    note.hidden = false;
    note.textContent = payload.detail
      || 'Rooms cannot be listed on this machine. A code still works.';
    return;
  }

  if (rooms.length) {
    note.hidden = true;
    return;
  }

  // Two different empty states. Before the first answer there is nothing to
  // conclude yet, and saying so beats showing "none found" for two seconds
  // every time this screen opens.
  note.hidden = false;
  note.textContent = state.discoverAnswered
    ? 'No rooms found. A room is listed only if its host set it to public and '
      + 'both machines are on the same network. A code works either way.'
    : 'Looking for rooms...';
}

async function pollDiscovery() {
  try {
    const response = await fetch('/api/room/discover', { cache: 'no-store' });
    if (!response.ok) {
      return;
    }

    const payload = await response.json();
    renderRooms(payload);
    state.discoverAnswered = true;
  } catch (error) {
    console.error('room discovery poll failed', error);
  }
}

function startDiscovery() {
  stopDiscovery();

  // initMultiplayer shows the chooser while the app is still starting, long
  // before anyone has asked for multiplayer. The first request is what binds
  // the discovery port in Python, so polling before the screen is open would
  // open a socket for a player who never leaves single player.
  if (!state.active) {
    return;
  }

  state.discoverAnswered = false;
  renderRooms({ listening: true, rooms: [] });
  pollDiscovery();
  state.discoverTimer = window.setInterval(pollDiscovery, DISCOVER_INTERVAL);
}

function stopDiscovery() {
  if (state.discoverTimer) {
    window.clearInterval(state.discoverTimer);
    state.discoverTimer = null;
  }
}

// -- polling ----------------------------------------------------------------

async function poll() {
  try {
    const response = await fetch('/api/room', { cache: 'no-store' });
    if (!response.ok) {
      return;
    }

    const snapshot = await response.json();
    state.snapshot = snapshot;
    state.mode = snapshot.mode;

    if (snapshot.mode === 'idle') {
      exitMatch();
      stopPolling();
      showPanel('choice');
      return;
    }

    // The host can close the room, or remove you from it, while you are looking
    // at it. Say which of those happened rather than emptying the screen.
    if (snapshot.closed_reason) {
      exitMatch();
      stopPolling();
      showPanel('choice');
      setNotice(snapshot.closed_reason.text, 'error');
      await call('/api/room/leave');
      return;
    }

    // The room's phase decides which screen this player is on. The host does
    // not tell anyone to go to the board; everybody notices that the room is no
    // longer waiting and goes.
    const phase = snapshot.room && snapshot.room.phase;
    if (phase && phase !== 'waiting') {
      if (!matchIsActive()) {
        enterMatch({
          playerId: snapshot.player_id,
          amHost: snapshot.mode === 'hosting',
          spectator: Boolean(snapshot.spectator),
          watching: snapshot.watching,
          onExit: function () {
            showPanel('lobby');
            poll();
          }
        });
      } else {
        // The host moves a spectator when the player it was following leaves,
        // so which stream this instance is on can change without it asking.
        // The board is told every poll rather than only on the way in.
        setWatching(snapshot.watching);
      }
      return;
    }

    // Waiting again, so any match that was running is over and everybody goes
    // back to the lobby. This is the only thing that ends the match screen, and
    // it is the same signal that started it. The board itself decides nothing
    // about when to close.
    if (matchIsActive()) {
      exitMatch();
    }

    renderLobby(snapshot);
  } catch (error) {
    console.error('room poll failed', error);
  }
}

function startPolling() {
  stopPolling();
  poll();
  state.pollTimer = window.setInterval(poll, POLL_INTERVAL);
}

function stopPolling() {
  if (state.pollTimer) {
    window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

// -- actions ----------------------------------------------------------------

async function hostRoom() {
  setNotice('');
  state.elements.hostButton.disabled = true;

  const result = await call('/api/room/host', {
    rules: state.rulesFor ? state.rulesFor() : {}
  });

  state.elements.hostButton.disabled = false;

  if (!result.ok) {
    setNotice(result.payload.detail || 'The room could not be opened.', 'error');
    return;
  }

  showPanel('lobby');
  startPolling();
}

// A join target is a code or an address, and the same endpoint accepts either.
// A room clicked in the list supplies its address here, so the two ways in are
// the same call and only differ in where the string came from.
async function attemptJoin(target, colourOverride, spectate) {
  setNotice('');
  state.lastTarget = target;
  state.lastSpectate = Boolean(spectate);
  state.elements.joinButton.disabled = true;
  state.elements.colourChoices.hidden = true;

  const body = { address: target, spectate: Boolean(spectate) };
  if (colourOverride) {
    body.colour = colourOverride;
  }

  const result = await call('/api/room/join', body);
  state.elements.joinButton.disabled = false;

  if (result.payload.ok) {
    showPanel('lobby');
    startPolling();
    return;
  }

  setNotice(result.payload.text || 'The host refused the join.', 'error');

  // A colour clash is the one refusal the player can fix in a single click, so
  // the free colours the host sent are offered as buttons. The swatches live
  // on the join panel, so a clash from a room in the list moves there, and the
  // address goes into the box with it so the retry has something to send.
  // A spectator is never refused for a colour, so the swatches are not offered
  // on that path: they would be a fix for a problem this refusal is not.
  const available = result.payload.available_colours;
  if (!spectate && Array.isArray(available) && available.length) {
    state.elements.address.value = target;
    showPanel('join');
    offerColours(available);
  }
}

function joinRoom(colourOverride) {
  return attemptJoin(
    state.elements.address.value,
    colourOverride,
    state.elements.spectate.checked
  );
}

function offerColours(colours) {
  const holder = state.elements.colourSwatches;
  holder.textContent = '';

  colours.forEach(function (colour) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'swatch';
    button.style.background = colour;
    button.setAttribute('aria-label', 'Join as ' + colour);
    button.addEventListener('click', function () {
      attemptJoin(state.lastTarget, colour, state.lastSpectate);
    });
    holder.appendChild(button);
  });

  state.elements.colourChoices.hidden = false;
}

async function copyCode() {
  const code = state.elements.code.textContent;
  if (!code) {
    return;
  }

  // The clipboard API is not available in every webview build, so a text area
  // and the old command are the fallback rather than an error.
  try {
    await window.navigator.clipboard.writeText(code);
    state.elements.copyButton.textContent = 'Copied';
  } catch (error) {
    const holder = document.createElement('textarea');
    holder.value = code;
    document.body.appendChild(holder);
    holder.select();
    try {
      document.execCommand('copy');
      state.elements.copyButton.textContent = 'Copied';
    } catch (fallbackError) {
      state.elements.copyButton.textContent = 'Select it';
    }
    holder.remove();
  }

  window.setTimeout(function () {
    state.elements.copyButton.textContent = 'Copy';
  }, 1600);
}

// -- editing the rules a room plays by --------------------------------------

// The host edits the room's rules from the lobby with the same editor the
// single player setup uses, in its multiplayer scope, so there is one rule
// editor in the application rather than two that can disagree about what a
// field means. What changes is where the answer goes: to the room, which sends
// it to everybody, instead of into the next solo match.
// Loading a setup fills the editor and changes nothing else. The room's rules
// move when Save rules is pressed, and not before, so looking at a setup is not
// the same act as imposing it on everybody already in the room.
async function renderSetups(rules) {
  const holder = state.elements.roomPresets;
  holder.textContent = 'Loading...';

  let presets;
  try {
    presets = (await listPresets()).presets || [];
  } catch (error) {
    holder.textContent = 'Saved setups could not be loaded.';
    return;
  }

  holder.textContent = '';

  if (!presets.length) {
    holder.textContent = 'Nothing saved yet. Set the rules up and name them '
      + 'below to reuse them next time.';
    return;
  }

  presets.forEach(function (preset) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'chip';
    chip.textContent = preset.name;
    chip.title = preset.builtin
      ? 'Shipped setup. Load it, change it, and save it under your own name.'
      : 'Your saved setup.';
    chip.addEventListener('click', function () {
      // Against the room's current rules, so a field the multiplayer scope does
      // not show keeps what the room already has rather than being quietly
      // replaced by whatever that setup happened to hold.
      const merged = Object.assign({}, rules, preset.rules);
      applyValues(state.elements.rulesEditor, state.schema, merged);

      holder.querySelectorAll('.chip').forEach(function (other) {
        other.classList.remove('is-loaded');
      });
      chip.classList.add('is-loaded');

      state.elements.setupName.value = preset.builtin ? '' : preset.name;
      setupStatus('Loaded ' + preset.name + '. Save rules to use it.');
    });
    holder.appendChild(chip);
  });
}

function setupStatus(message, failed) {
  const node = state.elements.setupStatus;
  node.textContent = message || '';
  node.classList.toggle('is-error', Boolean(failed));
}

async function saveSetup() {
  const rules = state.snapshot && state.snapshot.rules;
  if (!state.schema || !rules) {
    return;
  }

  const name = state.elements.setupName.value.trim();
  if (!name) {
    setupStatus('Give the setup a name first.', true);
    state.elements.setupName.focus();
    return;
  }

  // What is in the editor right now, which may not be what the room is on.
  // Saving what you can see is the only version of this that is not surprising.
  const values = readValues(state.elements.rulesEditor, state.schema, rules);

  state.elements.setupSave.disabled = true;
  const result = await call('/api/presets', { name: name, rules: values });
  state.elements.setupSave.disabled = false;

  if (result.payload.error) {
    setupStatus(result.payload.detail || 'That setup could not be saved.', true);
    return;
  }

  // The stored name, not the typed one. They differ when whitespace was
  // collapsed, and a setup saved under a name the player did not see is one
  // they will go looking for and not find.
  const stored = (result.payload && result.payload.name) || name;
  setupStatus(
    stored === name.trim()
      ? 'Saved as ' + stored + '.'
      : 'Saved as ' + stored + '. (The name was tidied up.)'
  );

  state.elements.setupName.value = '';
  await renderSetups(rules);

  // Highlighted, so it is obvious which of the chips is the one just saved.
  state.elements.roomPresets.querySelectorAll('.chip').forEach(function (chip) {
    if (chip.textContent === stored) {
      chip.classList.add('is-loaded');
      chip.scrollIntoView({ block: 'nearest' });
    }
  });
}

function openRules() {
  // The rules sit beside the room in the snapshot, not inside it. The room
  // carries only the two the lobby itself needs, the cap and the minimum.
  const rules = state.snapshot && state.snapshot.rules;
  if (!state.schema || !rules) {
    return;
  }

  state.rulesNotice('');

  // Built fresh each time it is opened, against the rules the room is on right
  // now. Building it once and reusing it would show whatever was last typed,
  // including a change that was cancelled or that another edit overwrote.
  buildEditor(
    state.elements.rulesEditor, state.schema, rules, 'multiplayer'
  );
  applyValues(state.elements.rulesEditor, state.schema, rules);

  state.elements.setupName.value = '';
  setupStatus('');
  renderSetups(rules);

  showPanel('rules');
}

async function saveRules() {
  const rules = state.snapshot && state.snapshot.rules;
  if (!state.schema || !rules) {
    return;
  }

  // The room's current rules are the base, so a field the multiplayer scope
  // does not show keeps the value the room already has rather than reverting
  // to a default nobody asked for.
  const values = readValues(state.elements.rulesEditor, state.schema, rules);

  state.elements.rulesSave.disabled = true;
  const result = await call('/api/room/rules', { rules: values });
  state.elements.rulesSave.disabled = false;

  if (result.payload.error === 'invalid_rules') {
    // Marked against the fields themselves rather than summarised in one line,
    // because a rule set can be wrong in several places at once and one
    // sentence can only name the first.
    showErrors(state.elements.rulesEditor, result.payload.errors || {});
    state.rulesNotice('Some of these cannot be used as they are.');
    return;
  }

  if (result.payload.error) {
    state.rulesNotice('Only the host can change the rules.');
    return;
  }

  state.snapshot = result.payload;
  showPanel('lobby');
  poll();
}

async function startMatch() {
  state.elements.startButton.disabled = true;

  const result = await call('/api/room/start');

  if (!result.payload.ok) {
    state.elements.startButton.disabled = false;
    setNotice(
      result.payload.text || 'The match could not be started.', 'error'
    );
    return;
  }

  // The screen change is left to the next poll, which reads the room's phase.
  // One path onto the board rather than two is what keeps the host and everyone
  // else arriving in the same state.
  poll();
}

async function toggleReady() {
  const room = state.snapshot && state.snapshot.room;
  if (!room) {
    return;
  }

  const me = room.players.find(function (player) {
    return player.id === state.snapshot.player_id;
  });

  await call('/api/room/ready', { ready: !(me && me.ready) });
  poll();
}

async function leaveRoom() {
  await call('/api/room/leave');
  stopPolling();
  state.snapshot = null;
  state.mode = 'idle';
  showPanel('choice');
  setNotice('');

  if (state.onLeave) {
    state.onLeave();
  }
}

// -- setup ------------------------------------------------------------------

export function initMultiplayer(root, options) {
  const panel = root.querySelector('[data-screen="multi"]');
  if (!panel) {
    return;
  }

  state.rulesFor = options.collectRules;
  state.onLeave = options.onLeave;
  state.schema = options.schema;
  state.rulesNotice = rulesNotice;

  state.elements = {
    panels: {
      choice: panel.querySelector('[data-multi="choice"]'),
      join: panel.querySelector('[data-multi="join"]'),
      lobby: panel.querySelector('[data-multi="lobby"]'),
      rules: panel.querySelector('[data-multi="rules"]')
    },
    notice: panel.querySelector('[data-multi-notice]'),
    hostButton: panel.querySelector('[data-multi-host]'),
    openJoin: panel.querySelector('[data-multi-open-join]'),
    backToChoice: panel.querySelector('[data-multi-back]'),
    address: panel.querySelector('[data-multi-address]'),
    joinButton: panel.querySelector('[data-multi-join]'),
    spectate: panel.querySelector('[data-multi-spectate]'),
    colourChoices: panel.querySelector('[data-multi-colours]'),
    colourSwatches: panel.querySelector('[data-multi-swatches]'),
    rooms: panel.querySelector('[data-multi-rooms]'),
    roomsNote: panel.querySelector('[data-multi-rooms-note]'),
    lobbyTitle: panel.querySelector('[data-lobby-title]'),
    codeBlock: panel.querySelector('[data-lobby-code-block]'),
    chosenCode: panel.querySelector('[data-lobby-chosen]'),
    code: panel.querySelector('[data-lobby-code]'),
    copyButton: panel.querySelector('[data-lobby-copy]'),
    advanced: panel.querySelector('[data-lobby-advanced]'),
    visibility: panel.querySelector('[data-lobby-visibility]'),
    connected: panel.querySelector('[data-lobby-connected]'),
    addresses: panel.querySelector('[data-lobby-addresses]'),
    players: panel.querySelector('[data-lobby-players]'),
    capacity: panel.querySelector('[data-lobby-capacity]'),
    watchers: panel.querySelector('[data-lobby-watchers]'),
    spectators: panel.querySelector('[data-lobby-spectators]'),
    spectatorNote: panel.querySelector('[data-lobby-spectator-note]'),
    rules: panel.querySelector('[data-lobby-rules]'),
    readyButton: panel.querySelector('[data-lobby-ready]'),
    leaveButton: panel.querySelector('[data-lobby-leave]'),
    startNote: panel.querySelector('[data-lobby-start-note]'),
    startButton: panel.querySelector('[data-lobby-start]'),
    hostOnly: panel.querySelector('[data-lobby-host-only]'),
    editRules: panel.querySelector('[data-lobby-edit-rules]'),
    rulesEditor: panel.querySelector('[data-room-rules-editor]'),
    rulesNotice: panel.querySelector('[data-room-rules-notice]'),
    rulesSave: panel.querySelector('[data-room-rules-save]'),
    rulesCancel: panel.querySelector('[data-room-rules-cancel]'),
    roomPresets: panel.querySelector('[data-room-presets]'),
    setupName: panel.querySelector('[data-room-setup-name]'),
    setupSave: panel.querySelector('[data-room-setup-save]'),
    setupStatus: panel.querySelector('[data-room-setup-status]')
  };

  state.elements.hostButton.addEventListener('click', hostRoom);
  state.elements.openJoin.addEventListener('click', function () {
    setNotice('');
    showPanel('join');
    state.elements.address.focus();
  });
  state.elements.backToChoice.addEventListener('click', function () {
    setNotice('');
    showPanel('choice');
  });
  state.elements.joinButton.addEventListener('click', function () {
    joinRoom(null);
  });
  state.elements.address.addEventListener('keydown', function (event) {
    if (event.key === 'Enter') {
      event.preventDefault();
      joinRoom(null);
    }
  });
  state.elements.copyButton.addEventListener('click', copyCode);
  state.elements.readyButton.addEventListener('click', toggleReady);
  state.elements.startButton.addEventListener('click', startMatch);
  state.elements.leaveButton.addEventListener('click', leaveRoom);
  state.elements.editRules.addEventListener('click', openRules);
  state.elements.rulesSave.addEventListener('click', saveRules);
  state.elements.setupSave.addEventListener('click', saveSetup);
  state.elements.rulesCancel.addEventListener('click', function () {
    showPanel('lobby');
  });

  showPanel('choice');
}

// Called when the Play tab shows the multiplayer screen, so a room that is
// already open is displayed rather than the chooser.
export function resumeMultiplayer() {
  state.active = true;
  poll().then(function () {
    if (state.mode !== 'idle') {
      showPanel('lobby');
      startPolling();
    }
  });
}

export function suspendMultiplayer() {
  state.active = false;
  stopPolling();
  stopDiscovery();
}
