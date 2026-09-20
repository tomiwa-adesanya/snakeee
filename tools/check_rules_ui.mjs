// Check that the editor and the validator agree about the rules.
//
// Run it from the project root:
//
//     python tools/check_rules_ui.py
//
// which generates the cases and then runs this. Running this file directly
// without that will fail, because the cases come from the Python side.
//
// The point is drift. Which options are irrelevant right now, and which are
// pinned by another setting, is answered twice: once in static/js/rules.js so
// the screen can hide and lock rows, and once in utils/game/rules.py so a saved
// setup arriving from an older build is settled rather than played as sent.
// Both read the same table, but they are separate code, and separate code
// drifts. If it ever does, the editor will show something the game will not
// honour, which is the worst kind of wrong: it looks like it worked.
//
// This is not a check of the screen. Whether a section is nice to use, whether
// the collapsed state is the right default, and whether a locked row reads as
// locked rather than as broken are questions for a person with the game open.

import { readFileSync } from 'fs';

import { forcedFor, isRelevant } from '../static/js/rules.js';

const casesPath = process.argv[2];
if (!casesPath) {
  console.log('No cases file given. Run tools/check_rules_ui.py instead.');
  process.exit(1);
}

const loaded = JSON.parse(readFileSync(casesPath, 'utf8'));
const schema = loaded.schema;
const cases = loaded.cases;

let failed = 0;
let checked = 0;

cases.forEach(function (entry) {
  const values = entry.rules;

  Object.keys(entry.relevant).forEach(function (key) {
    checked += 1;
    const mine = isRelevant(schema, key, values);
    if (mine !== entry.relevant[key]) {
      failed += 1;
      console.log('  FAIL  ' + entry.name + ': relevance of ' + key);
      console.log('          the editor says  ' + mine);
      console.log('          the game says    ' + entry.relevant[key]);
    }
  });

  Object.keys(entry.forced).forEach(function (key) {
    checked += 1;
    const rule = forcedFor(schema, key, values);
    const mine = rule ? rule.value : null;
    if (mine !== entry.forced[key]) {
      failed += 1;
      console.log('  FAIL  ' + entry.name + ': forced value of ' + key);
      console.log('          the editor says  ' + JSON.stringify(mine));
      console.log('          the game says    '
        + JSON.stringify(entry.forced[key]));
    }
  });
});

console.log('');
console.log(cases.length + ' rule sets, ' + checked + ' answers compared.');

if (failed === 0) {
  console.log('The editor and the game agree.');
} else {
  console.log(failed + ' disagreement' + (failed === 1 ? '' : 's') + '.');
  process.exitCode = 1;
}
