// Drive the rules editor and check what it shows.
//
// Run it from the project root:
//
//     node tools/check_editor.mjs
//
// This one needs jsdom, which is not part of the project and is not installed
// by anything here:
//
//     npm install jsdom
//
// Without it the script says so and exits without failing, so it can sit in a
// check sequence on a machine that has never run npm.
//
// What it is for: the editor decides which options are on screen, which are
// locked, and what a locked one is set to, and it decides that again after
// every keystroke. That is the most logic there has ever been in the client,
// and all of it is invisible in a test that only reads functions. This builds
// the real editor against the real schema in a real DOM and changes settings
// the way a person would.
//
// What it is not: a check of how any of it looks. Whether a collapsed section
// is the right default, whether a locked row reads as locked rather than as
// broken, and whether the whole screen is less overwhelming than it was are
// questions for a person with the game open.

import { execFileSync } from 'child_process';
import { createRequire } from 'module';

const require = createRequire(import.meta.url);

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (error) {
  console.log('jsdom is not installed, so this check cannot run.');
  console.log('  npm install jsdom');
  console.log('It is not needed to play the game, build it, or test it.');
  process.exit(0);
}

const dom = new JSDOM('<!doctype html><div id="editor"></div>');
global.window = dom.window;
global.document = dom.window.document;
global.Event = dom.window.Event;

// The schema comes from the game rather than from a copy kept here, so a field
// that is renamed or regrouped shows up as a failure rather than as a check
// quietly testing something that no longer exists.
const schema = JSON.parse(execFileSync('python3', [
  '-c',
  'import json; from utils.game.rules import schema_document;'
  + ' print(json.dumps(schema_document()))'
], { encoding: 'utf8', maxBuffer: 10e6 }));

const { applyValues, buildEditor, readValues } =
  await import('../static/js/rules.js');

const box = document.getElementById('editor');
const editor = buildEditor(box, schema, schema.defaults, 'multiplayer');

let failed = 0;

function check(description, got, want) {
  if (JSON.stringify(got) === JSON.stringify(want)) {
    console.log('  PASS  ' + description);
  } else {
    failed += 1;
    console.log('  FAIL  ' + description);
    console.log('          got  ' + JSON.stringify(got));
    console.log('          want ' + JSON.stringify(want));
  }
}

function heading(text) {
  console.log('\n' + text);
}

function row(key) {
  return box.querySelector('[data-field="' + key + '"]');
}

function control(key) {
  return box.querySelector('[name="' + key + '"]');
}

// Changed the way a person changes it, so that everything downstream of a
// change is exercised rather than called directly.
function set(key, value) {
  const node = control(key);
  if (node.type === 'checkbox') {
    node.checked = value;
  } else {
    node.value = value;
  }
  node.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
}

heading('The editor builds');

check('every option has a row', box.querySelectorAll('[data-field]').length > 20, true);
check(
  'and the advanced ones are behind a section',
  Array.from(box.querySelectorAll('[data-section]'))
    .map(function (node) { return node.dataset.section; })
    .includes('More options'),
  true
);
check(
  'which starts collapsed',
  Array.from(box.querySelectorAll('.rule-section-body'))
    .every(function (node) { return node.hidden; }),
  true
);

heading('A grid of arenas locks the edges');

editor.select('Arena');

check('one arena leaves them free', row('edge_behaviour').classList.contains('is-locked'), false);
check('and editable', control('edge_behaviour').disabled, false);

set('layout', '2x2');

check('a grid locks the row', row('edge_behaviour').classList.contains('is-locked'), true);
check('disables the control', control('edge_behaviour').disabled, true);
check('sets it to wrap', control('edge_behaviour').value, 'wrap');
check('and says why', box.querySelector('[data-reason="edge_behaviour"]').hidden, false);
check('what is read back is the locked value', readValues(box, schema, {}).edge_behaviour, 'wrap');

set('layout', '1x1');

check('going back to one arena frees it again', control('edge_behaviour').disabled, false);

heading('An option that cannot matter is not on screen');

editor.select('Match');
set('win_condition', 'first_to_score');

check('the score target is shown', row('score_target').hidden, false);
check('the kill target is not', row('kill_target').hidden, true);
check('nor the time limit', row('time_limit').hidden, true);

set('win_condition', 'timed');

check(
  'changing what the match is won on swaps them',
  [row('score_target').hidden, row('time_limit').hidden],
  [true, false]
);

heading('A hidden option keeps what was set');

set('win_condition', 'first_to_score');
set('score_target', '77');
set('win_condition', 'timed');

check('still there while hidden', control('score_target').value, '77');

set('win_condition', 'first_to_score');

check('and comes back as it was', readValues(box, schema, {}).score_target, 77);

heading('Items');

editor.select('Items');
set('poisons', 'off');
set('pickups', 'off');

check('which poisons exist is hidden when there are none', row('slow_poison').hidden, true);
check('and so is how long an effect lasts', row('effect_duration').hidden, true);

set('pickups', 'high');

check('either family brings the duration back', row('effect_duration').hidden, false);
check('but not the poison kinds', row('slow_poison').hidden, true);

heading('Loading a saved setup');

applyValues(box, schema, {
  layout: '2x2', edge_behaviour: 'walls', win_condition: 'timed',
});

check('one that asks for walls on a grid is corrected', control('edge_behaviour').value, 'wrap');
check('and the rows follow it', row('time_limit').hidden, false);
check('rather than staying as they were', row('score_target').hidden, true);

console.log('');
if (failed === 0) {
  console.log('All checks passed.');
} else {
  console.log(failed + ' check' + (failed === 1 ? '' : 's') + ' failed.');
  process.exitCode = 1;
}
