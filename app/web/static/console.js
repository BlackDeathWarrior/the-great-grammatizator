/* The console.
 *
 * Everything here exists because of one fact: the status region replaces
 * itself wholesale every two seconds (hx-swap="outerHTML"), and the same
 * partial comes back from all five action POSTs. Nothing in that region
 * survives a swap - not an open disclosure, not an animation's progress, not
 * a focused field.
 *
 * So the DOM is never the truth about what the operator opened. The store is,
 * and the DOM is redrawn from it after every swap.
 */
(function () {
  'use strict';

  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)');

  /* --- the brief: two front doors onto the same six fields ----------------
   *
   * Moved here from an inline script in index.html, unchanged in behaviour.
   * These stay on `window` deliberately: partials/interview.html calls
   * showLists() from an inline onclick, and that markup is swapped in by htmx
   * long after this file has run.
   */
  function setDisabled(host, state) {
    host.querySelectorAll('select').forEach(function (el) { el.disabled = state; });
  }

  function setMode(describing) {
    var interview = document.getElementById('interview');
    var lists = document.getElementById('lists');
    if (!interview || !lists) return;
    interview.classList.toggle('hidden', !describing);
    lists.classList.toggle('hidden', describing);
    var describe = document.getElementById('mode-describe');
    var pick = document.getElementById('mode-lists');
    if (describe) describe.setAttribute('aria-pressed', String(describing));
    if (pick) pick.setAttribute('aria-pressed', String(!describing));
    // A disabled select is not submitted, which is the only thing stopping the
    // hidden block sending a second value for every field.
    setDisabled(interview, !describing);
    setDisabled(lists, describing);
  }

  window.showInterview = function () { setMode(true); };
  window.showLists = function () { setMode(false); };

  /* --- sound -------------------------------------------------------------
   *
   * Synthesised, so there is no audio file to ship and nothing to 404. Off
   * until the operator asks for it, and silent under reduced motion: someone
   * who has told their machine to calm down has already answered this.
   *
   * There is no sound for failure. A machine that beeps at bad news teaches
   * the operator to mute it, and then the good signals are gone too.
   */
  var audio = {
    ctx: null,
    on: false,

    enabled: function () {
      return this.on && !reduced.matches;
    },

    resume: function () {
      if (!this.ctx) {
        var Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return null;
        this.ctx = new Ctx();
      }
      // Browsers start a context suspended until a gesture; every call site
      // here is downstream of a click, so this is the right place to wake it.
      if (this.ctx.state === 'suspended') this.ctx.resume();
      return this.ctx;
    },

    /* One shaped blip. Short attack, exponential tail - a struck object
       rather than a synthesiser pad. */
    blip: function (freq, dur, peak, type) {
      if (!this.enabled()) return;
      var ctx = this.resume();
      if (!ctx) return;
      var t = ctx.currentTime;
      var osc = ctx.createOscillator();
      var gain = ctx.createGain();
      osc.type = type || 'triangle';
      osc.frequency.setValueAtTime(freq, t);
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(peak, t + 0.004);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + dur);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(t);
      osc.stop(t + dur + 0.02);
    },

    key: function () { this.blip(320, 0.035, 0.05, 'square'); },
    tick: function () { this.blip(880, 0.06, 0.045, 'triangle'); },
    settle: function () {
      this.blip(587.33, 0.18, 0.05, 'triangle');
      var self = this;
      window.setTimeout(function () { self.blip(880, 0.26, 0.045, 'triangle'); }, 90);
    }
  };

  /* --- the lane store ----------------------------------------------------
   *
   * sessionStorage, not localStorage: which lanes were open is state about
   * one run in one tab, and carrying it to a different job next week would be
   * wrong. Not the URL either - this changes on every click, and the URL is
   * something the operator might share.
   */
  var store = {
    key: function () {
      var host = document.getElementById('job-status');
      var id = host && host.getAttribute('data-job');
      return id ? 'gg:lanes:' + id : null;
    },

    read: function () {
      var key = this.key();
      if (!key) return { mode: null, open: [] };
      try {
        var raw = window.sessionStorage.getItem(key);
        if (!raw) return { mode: null, open: [] };
        var parsed = JSON.parse(raw);
        return {
          mode: parsed.mode || null,
          open: Array.isArray(parsed.open) ? parsed.open : []
        };
      } catch (e) {
        // Private mode, disabled storage, corrupt JSON. The interface still
        // works from the server's defaults; it just forgets.
        return { mode: null, open: [] };
      }
    },

    write: function (state) {
      var key = this.key();
      if (!key) return;
      try {
        window.sessionStorage.setItem(key, JSON.stringify(state));
      } catch (e) { /* nothing to do; the run continues without memory */ }
    },

    toggle: function (id, open) {
      var state = this.read();
      var at = state.open.indexOf(id);
      if (open && at === -1) state.open.push(id);
      if (!open && at !== -1) state.open.splice(at, 1);
      // Once the operator has touched a lane, their arrangement is the truth
      // and the server default no longer reopens things behind them.
      if (state.mode === null) state.mode = 'custom';
      this.write(state);
    },

    setMode: function (mode) {
      var state = this.read();
      state.mode = mode;
      // An explicit collapse-all / expand-all also settles every lane, so the
      // per-lane list cannot immediately contradict the mode.
      state.open = mode === 'detail' ? laneIds() : [];
      this.write(state);
    }
  };

  function lanes() {
    return Array.prototype.slice.call(document.querySelectorAll('details.lane'));
  }

  function laneIds() {
    return lanes().map(function (el) { return el.id; });
  }

  /* Redraw the disclosure state from the store.
   *
   * The server has already rendered a sensible default (an artefact in trouble
   * opens itself). The operator's own choice, once made, wins over it.
   */
  function applyLaneState() {
    var state = store.read();
    if (state.mode === null) return;
    var known = state.open;

    lanes().forEach(function (el) {
      var wanted = known.indexOf(el.id) !== -1;
      if (el.open !== wanted) el.open = wanted;
    });
  }

  /* --- diegetic motion ---------------------------------------------------
   *
   * A lane flashes because its state token changed, never because the element
   * is new. Every element is new every two seconds; almost none of them have
   * just done anything.
   */
  var seenTokens = Object.create(null);
  var seenSettled = null;

  function markChanges() {
    var host = document.getElementById('job-status');
    if (!host) return;

    var first = Object.keys(seenTokens).length === 0;
    var moved = 0;

    lanes().forEach(function (el) {
      var token = el.getAttribute('data-state-token') || '';
      var id = el.id;
      var previous = seenTokens[id];
      seenTokens[id] = token;
      if (first || previous === undefined || previous === token) return;
      moved += 1;
      if (reduced.matches) return;
      // Restart the one-shot: removing the class and forcing layout lets the
      // same animation run again on the next real change.
      el.classList.remove('just-moved');
      void el.offsetWidth;
      // Staggered by position in the rack. Seven artefacts frequently settle
      // on the same poll, and seven simultaneous flashes read as one event
      // rather than as seven - which is the opposite of what the rack is for.
      el.style.setProperty('--moved-delay', (moved - 1) * 70 + 'ms');
      el.classList.add('just-moved');
    });

    if (moved > 0) audio.tick();

    // The job settling is its own event, and earns a different sound.
    var settled = host.getAttribute('aria-busy') === 'false';
    if (seenSettled === false && settled) audio.settle();
    seenSettled = settled;
  }

  /* --- wiring ------------------------------------------------------------ */

  // Delegated and bound once. Per-element listeners would leak thirty times a
  // minute against a region that is rebuilt on every poll. `toggle` does not
  // bubble, hence the capture phase.
  document.addEventListener('toggle', function (e) {
    var el = e.target;
    if (!el || !el.classList || !el.classList.contains('lane')) return;
    store.toggle(el.id, el.open);
  }, true);

  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;

    var fan = e.target.closest('[data-fan-lane]');
    if (fan) {
      e.preventDefault();
      var lane = document.getElementById('lane-' + fan.getAttribute('data-fan-lane'));
      if (lane) {
        lane.open = true;
        store.toggle(lane.id, true);
        lane.scrollIntoView({
          block: 'nearest',
          behavior: reduced.matches ? 'auto' : 'smooth'
        });
        audio.key();
      }
      return;
    }

    var density = e.target.closest('[data-density-set]');
    if (density) {
      e.preventDefault();
      var mode = density.getAttribute('data-density-set');
      store.setMode(mode);
      lanes().forEach(function (el) { el.open = mode === 'detail'; });
      audio.key();
      syncDensityButtons(mode);
      return;
    }

    var toggle = e.target.closest('[data-sound-toggle]');
    if (toggle) {
      e.preventDefault();
      audio.on = !audio.on;
      try { window.sessionStorage.setItem('gg:sound', audio.on ? '1' : '0'); } catch (err) {}
      syncSoundButtons();
      if (audio.on) audio.key();
    }
  });

  function syncDensityButtons(mode) {
    document.querySelectorAll('[data-density-set]').forEach(function (el) {
      el.setAttribute('aria-pressed', String(el.getAttribute('data-density-set') === mode));
    });
  }

  function syncSoundButtons() {
    var unavailable = reduced.matches;
    document.querySelectorAll('[data-sound-toggle]').forEach(function (el) {
      el.setAttribute('aria-pressed', String(audio.on && !unavailable));
      el.setAttribute(
        'title',
        unavailable
          ? 'Sound stays off while your system asks for reduced motion'
          : (audio.on ? 'Sound on' : 'Sound off')
      );
    });
  }

  // Each swapped-in brief arrives with its selects enabled, and the rack
  // arrives with every lane at the server's default. Put both back.
  document.body.addEventListener('htmx:afterSwap', function (e) {
    var id = e.target && e.target.id;
    if (id === 'interview') {
      var describe = document.getElementById('mode-describe');
      setMode(!describe || describe.getAttribute('aria-pressed') === 'true');
      return;
    }
    if (id === 'job-status') {
      applyLaneState();
      markChanges();
      syncDensityButtons(store.read().mode);
      syncSoundButtons();
    }
  });

  /* --- keyboard ----------------------------------------------------------
   *
   * Shortcuts only when the operator is not typing, and never with a modifier
   * held: a bare "1" is a shortcut, ctrl+1 belongs to the browser.
   */
  function typing(el) {
    if (!el) return false;
    var tag = el.tagName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable;
  }

  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (typing(document.activeElement)) return;

    var switches = document.querySelectorAll('.switchbank input[type="checkbox"]');

    if (switches.length && e.key >= '1' && e.key <= '9') {
      var index = parseInt(e.key, 10) - 1;
      if (index < switches.length) {
        e.preventDefault();
        switches[index].checked = !switches[index].checked;
        switches[index].dispatchEvent(new Event('change', { bubbles: true }));
      }
      return;
    }

    if (e.key === 'Enter' && switches.length) {
      var form = document.getElementById('job-form');
      if (form) {
        e.preventDefault();
        audio.key();
        if (form.requestSubmit) { form.requestSubmit(); } else { form.submit(); }
      }
      return;
    }

    if (document.getElementById('job-status') && (e.key === 'e' || e.key === 'c')) {
      e.preventDefault();
      var mode = e.key === 'e' ? 'detail' : 'scan';
      store.setMode(mode);
      lanes().forEach(function (el) { el.open = mode === 'detail'; });
      syncDensityButtons(mode);
      audio.key();
    }
  });

  /* --- the run rail ------------------------------------------------------ */

  function syncRail() {
    var switches = document.querySelectorAll('.switchbank input[type="checkbox"]');
    if (!switches.length) return;
    var picked = 0;
    switches.forEach(function (input, i) {
      var lamp = document.querySelector('.rail-lamp[data-slot="' + i + '"]');
      if (input.checked) picked += 1;
      if (lamp) lamp.classList.toggle('is-lit', input.checked);
    });
    var count = document.querySelector('[data-rail-count]');
    if (count) {
      count.textContent = picked === 0
        ? 'nothing selected'
        : picked + (picked === 1 ? ' format' : ' formats');
    }
    var run = document.querySelector('[data-rail-run]');
    if (run) run.disabled = picked === 0;
  }

  document.addEventListener('change', function (e) {
    if (e.target.closest && e.target.closest('.switchbank')) {
      syncRail();
      audio.key();
    }
  });

  /* --- drag and drop intake ---------------------------------------------- */

  function wireIntake() {
    var zone = document.querySelector('[data-dropzone]');
    var input = document.getElementById('file');
    if (!zone || !input) return;

    function showName() {
      var name = zone.querySelector('[data-drop-name]');
      var has = !!(input.files && input.files.length);
      if (name) name.textContent = has ? input.files[0].name : '';
      zone.classList.toggle('has-file', has);
    }

    ['dragenter', 'dragover'].forEach(function (name) {
      zone.addEventListener(name, function (e) {
        e.preventDefault();
        zone.classList.add('is-armed');
      });
    });

    ['dragleave', 'dragend'].forEach(function (name) {
      zone.addEventListener(name, function (e) {
        // Moving between children fires dragleave; only a real exit counts.
        if (name === 'dragleave' && e.relatedTarget && zone.contains(e.relatedTarget)) return;
        zone.classList.remove('is-armed');
      });
    });

    zone.addEventListener('drop', function (e) {
      e.preventDefault();
      zone.classList.remove('is-armed');
      var files = e.dataTransfer && e.dataTransfer.files;
      if (!files || !files.length) return;
      // Hand the real File to the existing input, so the ordinary form post
      // does the work and there is no second upload path to keep correct.
      try {
        var box = new DataTransfer();
        box.items.add(files[0]);
        input.files = box.files;
      } catch (err) {
        return;
      }
      showName();
      audio.key();
    });

    input.addEventListener('change', showName);
    showName();
  }

  /* --- start ------------------------------------------------------------- */

  function start() {
    // The interview owns the fields until the operator says otherwise.
    var lists = document.getElementById('lists');
    if (lists) setDisabled(lists, true);

    try { audio.on = window.sessionStorage.getItem('gg:sound') === '1'; } catch (e) {}
    syncSoundButtons();
    syncRail();
    wireIntake();
    applyLaneState();
    markChanges();
    syncDensityButtons(store.read().mode);

    if (reduced.addEventListener) {
      reduced.addEventListener('change', syncSoundButtons);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
