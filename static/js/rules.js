// Rule editor, generated from /api/rules/schema.
//
// Nothing about the options is described in this file: no labels, no bounds, no
// lists of choices. Adding an option means adding it to utils/game/rules.py, and
// it appears here with no frontend change at all.
//
// Three decisions about presentation, after the first version put every field
// on one screen and was unusable:
//
//   1. One group at a time, behind tabs. Twenty-one fields in a column is a
//      wall of text; five is a form.
//   2. Options that do not work yet are hidden by default, behind a toggle.
//      Showing a disabled Poisons control to someone who wants to play is
//      noise, however honestly it is labelled.
//   3. Numbers have both a slider and a typable box. A slider alone cannot be
//      set precisely and cannot be read at a glance.

let cachedSchema = null;

export async function fetchSchema() {
  if (cachedSchema) {
    return cachedSchema;
  }

  const response = await fetch('/api/rules/schema', { cache: 'no-store' });
  if (!response.ok) {
    throw new Error('the rule schema could not be loaded');
  }

  cachedSchema = await response.json();
  return cachedSchema;
}

export function humanise(value) {
  const words = String(value).split('_');
  return words
    .map(function (word, index) {
      if (index > 0) {
        return word;
      }
      return word.charAt(0).toUpperCase() + word.slice(1);
    })
    .join(' ');
}

function findField(schema, key) {
  for (let i = 0; i < schema.groups.length; i += 1) {
    const found = schema.groups[i].fields.find(function (field) {
      return field.key === key;
    });
    if (found) {
      return found;
    }
  }
  return null;
}

function valueOf(control, field) {
  if (field.type === 'bool') {
    return control.checked;
  }
  if (field.type === 'int') {
    return parseInt(control.value, 10);
  }
  if (field.type === 'float') {
    return parseFloat(control.value);
  }
  return control.value;
}

function isNumeric(field) {
  return field.type === 'int' || field.type === 'float';
}

// -- one row ----------------------------------------------------------------

function renderRow(field, value) {
  const row = document.createElement('div');
  row.className = 'rule';
  row.dataset.field = field.key;

  if (!field.active) {
    row.classList.add('is-inactive');
  }

  const label = document.createElement('label');
  label.className = 'rule-label';
  label.textContent = field.label;
  label.htmlFor = 'rule-' + field.key;

  if (!field.active) {
    const badge = document.createElement('span');
    badge.className = 'rule-badge';
    badge.textContent = 'Planned';
    label.appendChild(badge);
  }

  row.appendChild(label);
  row.appendChild(renderControls(field, value));

  if (field.help) {
    const help = document.createElement('p');
    help.className = 'rule-help';
    help.textContent = field.help;
    row.appendChild(help);
  }

  // Why this row is locked, when it is. Its own line rather than replacing the
  // help text, so the reason and what the option does are both readable.
  const reason = document.createElement('p');
  reason.className = 'rule-reason';
  reason.dataset.reason = field.key;
  reason.hidden = true;
  row.appendChild(reason);

  const error = document.createElement('p');
  error.className = 'rule-error';
  error.dataset.error = field.key;
  error.hidden = true;
  row.appendChild(error);

  return row;
}

function renderControls(field, value) {
  const wrap = document.createElement('div');
  wrap.className = 'rule-controls';

  if (field.type === 'bool') {
    const toggle = document.createElement('input');
    toggle.type = 'checkbox';
    toggle.className = 'rule-toggle';
    toggle.id = 'rule-' + field.key;
    toggle.name = field.key;
    toggle.checked = Boolean(value);
    toggle.disabled = !field.active;
    wrap.appendChild(toggle);
    return wrap;
  }

  if (field.type === 'enum') {
    // Wrapped, because the native select arrow sits hard against the right
    // edge of the box. The wrapper draws it with real space around it; see
    // .select in base.css.
    const holder = document.createElement('span');
    holder.className = 'select';

    const select = document.createElement('select');
    select.id = 'rule-' + field.key;
    select.name = field.key;
    select.disabled = !field.active;
    field.options.forEach(function (option) {
      const node = document.createElement('option');
      node.value = option;
      node.textContent = humanise(option);
      select.appendChild(node);
    });
    select.value = value;

    holder.appendChild(select);
    wrap.appendChild(holder);
    return wrap;
  }

  if (field.type === 'text') {
    const text = document.createElement('input');
    text.type = 'text';
    text.id = 'rule-' + field.key;
    text.name = field.key;
    text.maxLength = field.max_length;
    text.value = value === undefined ? '' : value;
    text.disabled = !field.active;
    wrap.appendChild(text);
    return wrap;
  }

  // Numeric: slider plus a box that accepts typing. Both carry the field name,
  // and readValues takes the last one it finds, so they must stay in step.
  const slider = document.createElement('input');
  slider.type = 'range';
  slider.className = 'rule-slider';
  slider.id = 'rule-' + field.key;
  slider.name = field.key;
  slider.min = field.min;
  slider.max = field.max;
  slider.step = field.step;
  slider.value = value;
  slider.disabled = !field.active;

  const box = document.createElement('input');
  box.type = 'number';
  box.className = 'rule-number mono';
  box.name = field.key;
  box.min = field.min;
  box.max = field.max;
  box.step = field.step;
  box.value = value;
  box.disabled = !field.active;
  box.setAttribute('aria-label', field.label);

  slider.addEventListener('input', function () {
    box.value = slider.value;
  });
  box.addEventListener('input', function () {
    slider.value = box.value;
  });

  wrap.appendChild(slider);
  wrap.appendChild(box);

  if (field.unit) {
    const unit = document.createElement('span');
    unit.className = 'rule-unit';
    unit.textContent = field.unit;
    wrap.appendChild(unit);
  }

  return wrap;
}

// A collapsible subsection inside a group, made on first use.
//
// Collapsed to start. The whole point is that a tab opens showing a few things
// rather than twenty, and a section that started open would put the twenty
// back.
function sectionIn(panel, made, name, last) {
  if (made[name]) {
    return made[name].body;
  }

  const holder = document.createElement('section');
  holder.className = 'rule-section';
  holder.dataset.section = name;

  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'rule-section-toggle';
  toggle.setAttribute('aria-expanded', 'false');

  const caret = document.createElement('span');
  caret.className = 'rule-caret';
  caret.setAttribute('aria-hidden', 'true');
  toggle.appendChild(caret);

  const title = document.createElement('span');
  title.textContent = name;
  toggle.appendChild(title);

  const count = document.createElement('span');
  count.className = 'rule-section-count mono';
  toggle.appendChild(count);

  const body = document.createElement('div');
  body.className = 'rule-section-body';
  body.hidden = true;

  toggle.addEventListener('click', function () {
    const open = body.hidden;
    body.hidden = !open;
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    holder.classList.toggle('is-open', open);
  });

  holder.appendChild(toggle);
  holder.appendChild(body);

  // "More options" is always the last thing in a tab, whatever order the
  // fields happened to arrive in.
  if (last || !panel.querySelector('[data-section="More options"]')) {
    panel.appendChild(holder);
  } else {
    panel.insertBefore(
      holder, panel.querySelector('[data-section="More options"]')
    );
  }

  made[name] = { holder: holder, body: body, count: count };
  return body;
}

// -- what depends on what ---------------------------------------------------
//
// The tables come from the server with the schema, so the screen and the
// validator cannot drift apart. Two relationships, and the difference is what
// the player sees.
//
// **needs** is irrelevance: the option has no effect at all right now, so its
// row is hidden. A greyed-out row that cannot matter is noise, and a screenful
// of them reads as something being broken. The stored value is untouched, so
// turning the parent back on brings back what was set.
//
// **forced** is a rule of the game: the option would have an effect, but this
// combination is not allowed. Shown, locked, set, and told why. Hiding it would
// leave a player wondering where a setting they chose had gone.

function anyMet(conditions, values) {
  return (conditions || []).some(function (condition) {
    return condition.in.indexOf(values[condition.key]) !== -1;
  });
}

export function isRelevant(schema, key, values) {
  const conditions = (schema.needs || {})[key];
  return !conditions || anyMet(conditions, values);
}

export function forcedFor(schema, key, values) {
  const rule = (schema.forced || {})[key];
  if (!rule || !anyMet(rule.when, values)) {
    return null;
  }
  return rule;
}

// -- the editor -------------------------------------------------------------

// scope is 'solo' or 'multiplayer'. Returns a controller so the caller can
// switch tabs or reveal the not-yet-working options without touching the DOM.
export function buildEditor(container, schema, rules, scope) {
  container.textContent = '';

  const tabs = document.createElement('div');
  tabs.className = 'rule-tabs';
  tabs.setAttribute('role', 'tablist');

  const panels = document.createElement('div');
  panels.className = 'rule-panels';

  const groups = [];

  schema.groups.forEach(function (group) {
    const fields = group.fields.filter(function (field) {
      return field.scope === 'both' || field.scope === scope;
    });

    if (fields.length === 0) {
      return;
    }

    const activeCount = fields.filter(function (field) {
      return field.active;
    }).length;

    const tab = document.createElement('button');
    tab.type = 'button';
    tab.className = 'rule-tab';
    tab.dataset.tab = group.name;
    tab.setAttribute('role', 'tab');
    tab.textContent = group.name;

    if (activeCount === 0) {
      tab.classList.add('is-later');
    }

    const panel = document.createElement('div');
    panel.className = 'rule-panel';
    panel.dataset.panelGroup = group.name;
    panel.hidden = true;

    // Three places a row can land: straight into the panel, into a named
    // subsection, or into the group's own "More options". Basic first, so the
    // top of every tab is the handful of things most people change and nothing
    // else.
    const sections = {};

    function holderFor(field) {
      if (field.section) {
        return sectionIn(panel, sections, field.section, false);
      }
      if (field.tier === 'basic') {
        return panel;
      }
      return sectionIn(panel, sections, 'More options', true);
    }

    fields.slice().sort(function (left, right) {
      const rank = function (field) {
        if (field.tier === 'basic' && !field.section) {
          return 0;
        }
        return field.section ? 1 : 2;
      };
      return rank(left) - rank(right);
    }).forEach(function (field) {
      const value = rules[field.key] === undefined
        ? field.default
        : rules[field.key];
      holderFor(field).appendChild(renderRow(field, value));
    });

    tab.addEventListener('click', function () {
      select(group.name);
    });

    tabs.appendChild(tab);
    panels.appendChild(panel);
    groups.push({
      name: group.name, tab: tab, panel: panel,
      active: activeCount, sections: sections,
    });
  });

  container.appendChild(tabs);
  container.appendChild(panels);

  function select(name) {
    groups.forEach(function (group) {
      const chosen = group.name === name;
      group.tab.classList.toggle('is-selected', chosen);
      group.tab.setAttribute('aria-selected', chosen ? 'true' : 'false');
      group.panel.hidden = !chosen;
    });
  }

  function setShowInactive(show) {
    container.classList.toggle('shows-inactive', show);
    groups.forEach(function (group) {
      if (group.active === 0) {
        group.tab.hidden = !show;
      }
    });

    if (!show) {
      const selected = groups.find(function (group) {
        return group.tab.classList.contains('is-selected');
      });
      if (!selected || selected.active === 0) {
        const fallback = groups.find(function (group) {
          return group.active > 0;
        });
        if (fallback) {
          select(fallback.name);
        }
      }
    }
  }

  // -- keeping the screen honest ------------------------------------------
  //
  // Run after every change to anything. Cheap enough to do wholesale rather
  // than working out which rows one edit could possibly affect: the whole
  // editor is a few dozen rows, and a partial update that missed a chain would
  // leave a row on screen that cannot matter, which is the exact thing this
  // exists to prevent.

  function refresh() {
    const values = readValues(container, schema, rules);

    schema.groups.forEach(function (group) {
      group.fields.forEach(function (field) {
        const row = container.querySelector(
          '[data-field="' + field.key + '"]'
        );
        if (!row) {
          return;
        }

        row.hidden = !isRelevant(schema, field.key, values);

        const rule = forcedFor(schema, field.key, values);
        const reason = row.querySelector('[data-reason]');

        row.classList.toggle('is-locked', Boolean(rule));
        if (reason) {
          reason.textContent = rule ? rule.reason : '';
          reason.hidden = !rule;
        }

        row.querySelectorAll('[name]').forEach(function (control) {
          // A field that is not implemented yet stays disabled whatever the
          // dependencies say, which is why this reads both.
          control.disabled = !field.active || Boolean(rule);

          if (rule) {
            if (field.type === 'bool') {
              control.checked = Boolean(rule.value);
            } else {
              control.value = rule.value;
            }
          }
        });
      });
    });

    // A collapsed section says how many of its options are showing, so it is
    // possible to tell an empty one from one worth opening without opening it.
    groups.forEach(function (group) {
      Object.keys(group.sections).forEach(function (name) {
        const section = group.sections[name];
        const rows = Array.prototype.filter.call(
          section.body.querySelectorAll('[data-field]'),
          function (row) {
            return !row.hidden;
          }
        );

        section.count.textContent = rows.length ? String(rows.length) : '';
        section.holder.hidden = rows.length === 0;
      });
    });
  }

  container.addEventListener('change', refresh);
  container.addEventListener('input', refresh);

  const first = groups.find(function (group) {
    return group.active > 0;
  });
  if (first) {
    select(first.name);
  }

  refresh();

  return { select: select, setShowInactive: setShowInactive, refresh: refresh };
}

// Read the rendered fields back. Values the editor did not show are preserved
// from base, so saving a preset from the solo screen does not quietly reset
// every multiplayer option to its default.
export function readValues(container, schema, base) {
  const values = Object.assign({}, base || {});

  container.querySelectorAll('[name]').forEach(function (control) {
    if (control.type === 'number') {
      return;
    }
    const field = findField(schema, control.name);
    if (field) {
      values[field.key] = valueOf(control, field);
    }
  });

  return values;
}

export function applyValues(container, schema, rules) {
  Object.keys(rules).forEach(function (key) {
    const field = findField(schema, key);
    if (!field) {
      return;
    }

    container.querySelectorAll('[name="' + key + '"]').forEach(function (node) {
      if (field.type === 'bool') {
        node.checked = Boolean(rules[key]);
      } else {
        node.value = rules[key];
      }
    });
  });

  // Setting .value in code fires nothing, so loading a preset would leave the
  // rows that depend on it showing the last setup's state. Announced here
  // rather than left to every caller to remember.
  container.dispatchEvent(new Event('change', { bubbles: true }));
}

export function showErrors(container, errors) {
  let firstBad = null;

  container.querySelectorAll('[data-error]').forEach(function (node) {
    const key = node.dataset.error;
    const message = errors ? errors[key] : null;

    node.textContent = message || '';
    node.hidden = !message;

    const row = container.querySelector('[data-field="' + key + '"]');
    if (row) {
      row.classList.toggle('has-error', Boolean(message));
      if (message && !firstBad) {
        firstBad = row;
      }
    }
  });

  return firstBad;
}

// A one-line summary of a ruleset, for preset cards. Reads from the schema so
// a renamed option does not leave a stale label behind.
export function summarise(schema, rules) {
  const parts = [
    rules.arena_width + 'x' + rules.arena_height,
    rules.edge_behaviour === 'wrap' ? 'wrap' : 'walls',
    'speed ' + rules.base_speed,
    humanise(rules.food_density).toLowerCase() + ' food'
  ];

  // The size is per arena, so a card that showed 70x70 and nothing else would
  // be describing a quarter of what a 2x2 preset actually opens.
  if (rules.layout && rules.layout !== '1x1') {
    parts.splice(1, 0, rules.layout + ' grid');
  }

  if (!rules.length_affects_speed) {
    parts.push('constant speed');
  }

  return parts;
}
