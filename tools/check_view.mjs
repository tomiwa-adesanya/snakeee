// Check the arithmetic of the board window, without a browser.
//
// Run it with no arguments, from the project root:
//
//     node tools/check_view.mjs
//
// static/js/view.js decides which part of the cell space one player is looking
// at, what lies past each edge of it, which edges are walls, and where else the
// same body has to be drawn because the space wraps. All of that is arithmetic
// with no canvas in it, which is why it lives in a module of its own and why it
// can be checked here rather than by staring at a running game and hoping.
//
// What this cannot check is the drawing. Whether the palettes read apart at a
// glance, whether the edge glow says what it means, and whether a crossing
// looks right are questions for a person with the game open.
//
// Node is the only thing here that is not Python. It is not needed to run the
// game, to build it, or to run the test suite. This directory is not imported
// by the application and can be deleted before a release.

import {
  arenaCount,
  boundsOf,
  drawOffsets,
  edgeColours,
  originOf,
  readArena,
  visible,
  wallSides
} from '../static/js/view.js';

let failed = 0;

function check(description, got, want) {
  const passed = JSON.stringify(got) === JSON.stringify(want);
  if (passed) {
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

// The four palettes, by the id of the arena that carries each.
const VERDANT = '#3ecf8e';
const EMBER = '#f0b950';
const ABYSS = '#58a6ff';
const BLOOM = '#c878f0';

const single = readArena({ w: 40, h: 40, edge: 'wrap' });
const wide = readArena({ w: 48, h: 24, edge: 'wrap', aw: 24, ah: 24, cols: 2, rows: 1 });
const quad = readArena({ w: 48, h: 48, edge: 'wrap', aw: 24, ah: 24, cols: 2, rows: 2 });
const walled = readArena({ w: 48, h: 48, edge: 'walls', aw: 24, ah: 24, cols: 2, rows: 2 });

heading('Reading the layout');

check('a single arena fills its own layout in', [single.aw, single.ah, single.cols, single.rows], [40, 40, 1, 1]);
check('a 2x2 layout is four arenas', arenaCount(quad), 4);
check('arena 0 starts at the origin', originOf(quad, 0), { arena: 0, x: 0, y: 0 });
check('arena 1 is one arena to the right', originOf(quad, 1), { arena: 1, x: 24, y: 0 });
check('arena 2 is one arena down', originOf(quad, 2), { arena: 2, x: 0, y: 24 });
check('arena 3 is across and down', originOf(quad, 3), { arena: 3, x: 24, y: 24 });
check('an id past the end is clamped rather than trusted', originOf(quad, 9), { arena: 3, x: 24, y: 24 });

heading('What lies past each edge');

check('right of Verdant is Ember', edgeColours(quad, 0).right, EMBER);
check('below Verdant is Abyss', edgeColours(quad, 0).bottom, ABYSS);
check('left of Verdant wraps round to Ember', edgeColours(quad, 0).left, EMBER);
check('above Verdant wraps round to Abyss', edgeColours(quad, 0).top, ABYSS);
check('right of Bloom wraps round to Abyss', edgeColours(quad, 3).right, ABYSS);
check('below Bloom wraps round to Ember', edgeColours(quad, 3).bottom, EMBER);
check('and Verdant is still Verdant', edgeColours(quad, 2).top, VERDANT);
check('one arena leads nowhere but itself', edgeColours(single, 0), { left: null, right: null, top: null, bottom: null });
check('a row of two has nothing above or below', [edgeColours(wide, 0).top, edgeColours(wide, 0).bottom], [null, null]);
check('but leads sideways in both directions', [edgeColours(wide, 0).left, edgeColours(wide, 0).right], [EMBER, EMBER]);

heading('Walls, which are only ever on the outside');

check('the outside of the grid glows at nothing', edgeColours(walled, 0), { left: null, right: EMBER, top: null, bottom: ABYSS });
check('arena 0 is walled left and top', wallSides(walled, 0), { left: true, right: false, top: true, bottom: false });
check('arena 3 is walled right and bottom', wallSides(walled, 3), { left: false, right: true, top: false, bottom: true });
check('a wrapping grid has no walls at all', wallSides(quad, 0), { left: false, right: false, top: false, bottom: false });

heading('Where else the same body has to be drawn');

check('a wrapping space is drawn nine ways', drawOffsets(quad).length, 9);
check('a walled one is drawn once', drawOffsets(walled).length, 1);

const crossing = boundsOf([{ x: 25, y: 10 }, { x: 24, y: 10 }, { x: 23, y: 10 }]);
check('the bounding box of a crossing body', crossing, { minX: 23, maxX: 25, minY: 10, maxY: 10 });
check('it is visible from the arena it is leaving', visible(quad, originOf(quad, 0), crossing, 0, 0), true);
check('and from the arena it is entering', visible(quad, originOf(quad, 1), crossing, 0, 0), true);
check('and not from the arena below either of them', visible(quad, originOf(quad, 2), crossing, 0, 0), false);

const far = boundsOf([{ x: 47, y: 10 }]);
check('the far side of the world is not visible where it is', visible(quad, originOf(quad, 0), far, 0, 0), false);
check('and is visible one width away, which is the wrap', visible(quad, originOf(quad, 0), far, -48, 0), true);

console.log('');
if (failed === 0) {
  console.log('All checks passed.');
} else {
  console.log(failed + ' check' + (failed === 1 ? '' : 's') + ' failed.');
  process.exitCode = 1;
}
