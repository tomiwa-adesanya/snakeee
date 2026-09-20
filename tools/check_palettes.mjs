// Check that every theme keeps the promises the game relies on.
//
// Run it from the project root:
//
//     node tools/check_palettes.mjs
//
// A theme is not only decoration here. The arena colours carry information --
// which arena you are standing in, which one lies past an edge -- and they sit
// underneath twelve fixed player colours that were chosen against a dark
// background and cannot move. So a new theme can be wrong in ways that are not
// a matter of taste, and those are what this checks:
//
//   Four arenas per set, since a 2x2 layout needs four.
//   Four different patterns, because the pattern is what a colourblind player
//   is left with when the colour stops carrying anything.
//   Every one of the twelve player colours readable on every background.
//   Food readable on every background.
//
// What it cannot check is whether a theme is pleasant, or whether the accents
// look different to you. Three of the four sets separate their arenas by hue
// rather than by brightness, which a contrast ratio cannot see -- that is what
// the patterns are for, and it is why "mono" exists as the set that separates
// by brightness instead.

import { readFileSync } from 'fs';

import {
  THEME_SETS,
  THEME_TEXTURE,
  themeFor,
  useThemeSet
} from '../static/js/render.js';

// Read from the profile store rather than copied, so a change to the player
// colours shows up here rather than being checked against a stale list.
const source = readFileSync('utils/store/profile.py', 'utf8');
const players = Array.from(source.matchAll(/"(#[0-9a-fA-F]{6})"/g))
  .map(function (match) { return match[1]; });

function luminance(hex) {
  const parts = [1, 3, 5].map(function (at) {
    let channel = parseInt(hex.slice(at, at + 2), 16) / 255;
    channel = channel <= 0.03928
      ? channel / 12.92
      : Math.pow((channel + 0.055) / 1.055, 2.4);
    return channel;
  });
  return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2];
}

function contrast(one, two) {
  const a = luminance(one);
  const b = luminance(two);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

let failed = 0;

function check(description, passed, detail) {
  if (passed) {
    console.log('  PASS  ' + description);
  } else {
    failed += 1;
    console.log('  FAIL  ' + description);
    if (detail) {
      console.log('          ' + detail);
    }
  }
}

if (players.length !== 12) {
  console.log('Expected twelve player colours, found ' + players.length + '.');
  console.log('If the list changed, this check needs looking at.');
  failed += 1;
}

Object.keys(THEME_SETS).forEach(function (name) {
  const set = THEME_SETS[name];
  console.log('\n' + name);

  check('four arenas', set.length === 4, 'found ' + set.length);

  const patterns = new Set(set.map(function (theme) { return theme.pattern; }));
  check(
    'four different patterns',
    patterns.size === 4,
    Array.from(patterns).join(', ')
  );

  const unreadable = [];
  set.forEach(function (theme) {
    players.forEach(function (player) {
      const ratio = contrast(player, theme.background);
      if (ratio < 3.0) {
        unreadable.push(player + ' on ' + theme.name + ' at '
          + ratio.toFixed(2) + ':1');
      }
    });
  });
  check(
    'every player colour reads on every background',
    unreadable.length === 0,
    unreadable.slice(0, 4).join('; ')
  );

  const dimFood = set.filter(function (theme) {
    return contrast(theme.food, theme.background) < 3.0;
  }).map(function (theme) { return theme.name; });
  check('food reads on every background', dimFood.length === 0, dimFood.join(', '));

  // Reported rather than checked. A ratio only sees brightness, so a set whose
  // arenas differ by hue scores low here and is still perfectly legible. It is
  // printed because "mono" is the set that is supposed to separate by
  // brightness, and for that one the number is the point.
  let closest = Infinity;
  for (let i = 0; i < set.length; i += 1) {
    for (let j = i + 1; j < set.length; j += 1) {
      closest = Math.min(closest, contrast(set[i].border, set[j].border));
    }
  }
  console.log('        closest pair of accents by brightness: '
    + closest.toFixed(2) + ':1');

  if (name === 'mono') {
    check(
      'and mono separates its arenas by brightness, which is its whole job',
      closest >= 1.8,
      closest.toFixed(2) + ':1 is too close to name from memory'
    );
  }
});

console.log('\nTelling the themes apart');

// The check that was missing, and the reason a whole theme shipped looking like
// another one. Everything above asks whether a theme works on its own. None of
// it asked whether two of them look different, so classic and earth sat at
// almost the same background lightness with almost the same grid strength and
// the same pattern alpha, separated only by the hue of a near-black -- which is
// the weakest signal there is at that lightness.
//
// Three axes, because relying on any one of them is how this happened.

function meanBackground(name) {
  const set = THEME_SETS[name];
  return set.reduce(function (total, theme) {
    return total + luminance(theme.background);
  }, 0) / set.length;
}

function gridAlpha(name) {
  const found = THEME_SETS[name][0].grid.match(/([\d.]+)\s*\)$/);
  return found ? Number(found[1]) : 0;
}

const names = Object.keys(THEME_SETS);

names.forEach(function (name) {
  console.log('        ' + name.padEnd(9)
    + ' background ' + meanBackground(name).toFixed(4)
    + '  grid ' + gridAlpha(name).toFixed(2)
    + '  texture ' + THEME_TEXTURE[name].alpha.toFixed(2)
    + ' at ' + THEME_TEXTURE[name].scale.toFixed(1) + 'x');
});

const alike = [];
for (let i = 0; i < names.length; i += 1) {
  for (let j = i + 1; j < names.length; j += 1) {
    const one = names[i];
    const two = names[j];

    // Any one of the three being clearly different is enough. Two themes may
    // legitimately share a lightness if their texture is nothing alike.
    const byLight = Math.abs(meanBackground(one) - meanBackground(two)) >= 0.004;
    const byGrid = Math.abs(gridAlpha(one) - gridAlpha(two)) >= 0.03;
    const byTexture =
      Math.abs(THEME_TEXTURE[one].alpha - THEME_TEXTURE[two].alpha) >= 0.03
      || Math.abs(THEME_TEXTURE[one].scale - THEME_TEXTURE[two].scale) >= 0.25;

    if (!byLight && !byGrid && !byTexture) {
      alike.push(one + ' and ' + two);
    }
  }
}

check(
  'every pair of themes differs in lightness, grid or texture',
  alike.length === 0,
  alike.join('; ')
);

check(
  'every theme has a texture of its own defined',
  names.every(function (name) { return Boolean(THEME_TEXTURE[name]); }),
  names.filter(function (name) { return !THEME_TEXTURE[name]; }).join(', ')
);

console.log('\nChoosing a set');

useThemeSet('mono');
check('a named set is used', themeFor(0).name === 'Iron', themeFor(0).name);

useThemeSet('nonsense');
check(
  'an unknown name falls back rather than breaking',
  themeFor(0).name === 'Verdant',
  themeFor(0).name
);

console.log('');
if (failed === 0) {
  console.log('All checks passed.');
} else {
  console.log(failed + ' check' + (failed === 1 ? '' : 's') + ' failed.');
  process.exitCode = 1;
}
