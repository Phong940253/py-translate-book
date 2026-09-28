/* EPUB reader client: chapter nav, paragraph jump/highlight, appearance
   settings and read-aloud (local TTS via /api/tts).

   Bootstrapped from window.APP.reader (set by reader.html); UI strings come
   from window.APP.i18n. Reading position and appearance are per-book and are
   kept in localStorage so the book reopens where you left off. */
(function () {
  'use strict';

  var APP = window.APP || {};
  var R = APP.reader || {};
  var path = R.path || '';

  function i18n(key) { return (APP.i18n && APP.i18n[key]) || key; }
  function fmt(key, vars) {
    var s = i18n(key);
    vars = vars || {};
    Object.keys(vars).forEach(function (k) { s = s.split('{' + k + '}').join(String(vars[k])); });
    return s;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function el(id) { return document.getElementById(id); }

  function getJSON(url) {
    return fetch(url).then(function (res) {
      return res.text().then(function (raw) {
        var data = null;
        try { data = JSON.parse(raw); } catch (e) { /* non-JSON */ }
        if (!res.ok) throw new Error((data && data.error) || ('HTTP ' + res.status));
        return data;
      });
    });
  }

  /* ------------------------------------------------------------- state */
  var state = { meta: null, chapter: null, items: [], ch: 1, cur: 0 };
  var settings = { family: 'serif', size: 18, lh: 1.75, width: 760, theme: 'dark' };
  var ttsCfg = { rate: 1, voice: null };
  var posKey = 'rd.pos:' + path;
  var setKey = 'rd.set:' + path;
  var ttsKey = 'rd.tts';

  function loadJSON(key, fallback) {
    try {
      var raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : fallback;
    } catch (e) { return fallback; }
  }
  function saveJSON(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* quota */ }
  }

  /* --------------------------------------------------------- appearance */
  function applySettings() {
    var rd = el('rd');
    if (!rd) return;
    rd.style.setProperty('--rd-width', settings.width + 'px');
    rd.style.setProperty('--rd-size', settings.size + 'px');
    rd.style.setProperty('--rd-lh', settings.lh);
    rd.setAttribute('data-family', settings.family);
    var stage = document.querySelector('.rd-stage');
    if (stage) stage.setAttribute('data-theme', settings.theme);

    el('rd-set-family').value = settings.family;
    el('rd-set-size').value = settings.size;
    el('rd-set-size-v').textContent = settings.size + 'px';
    el('rd-set-lh').value = settings.lh;
    el('rd-set-lh-v').textContent = Number(settings.lh).toFixed(2);
    el('rd-set-width').value = settings.width;
    el('rd-set-width-v').textContent = settings.width + 'px';
    document.querySelectorAll('.rd-theme').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.dataset.theme === settings.theme));
    });
  }

  function bindSettings() {
    el('rd-set-family').addEventListener('change', function () {
      settings.family = this.value; saveJSON(setKey, settings); applySettings();
    });
    el('rd-set-size').addEventListener('input', function () {
      settings.size = Number(this.value); saveJSON(setKey, settings); applySettings();
    });
    el('rd-set-lh').addEventListener('input', function () {
      settings.lh = Number(this.value); saveJSON(setKey, settings); applySettings();
    });
    el('rd-set-width').addEventListener('input', function () {
      settings.width = Number(this.value); saveJSON(setKey, settings); applySettings();
    });
    document.querySelectorAll('.rd-theme').forEach(function (b) {
      b.addEventListener('click', function () {
        settings.theme = b.dataset.theme; saveJSON(setKey, settings); applySettings();
      });
    });
    el('rd-set-reset').addEventListener('click', function () {
      settings = { family: 'serif', size: 18, lh: 1.75, width: 760, theme: 'dark' };
      saveJSON(setKey, settings); applySettings();
    });
  }

  /* --------------------------------------------------------------- TOC */
  function chapterLabel(c) {
    var title = c.title || '';
    if (title.length > 70) title = title.slice(0, 67) + '…';
    return c.n + '. ' + title;
  }

  function buildToc() {
    var meta = state.meta;
    var sel = el('rd-chapter');
    var toc = el('rd-toc');
    sel.textContent = '';
    toc.textContent = '';
    el('rd-toc-count').textContent = '(' + meta.total + ')';

    meta.chapters.forEach(function (c) {
      var opt = document.createElement('option');
      opt.value = c.n;
      opt.textContent = chapterLabel(c);
      sel.appendChild(opt);

      var b = document.createElement('button');
      b.type = 'button';
      b.dataset.n = c.n;
      b.textContent = chapterLabel(c);
      b.addEventListener('click', function () { goChapter(c.n, 0, true); togglePanel('toc'); });
      toc.appendChild(b);
    });
  }

  function markToc() {
    var sel = el('rd-chapter');
    if (sel) sel.value = String(state.ch);
    document.querySelectorAll('#rd-toc button').forEach(function (b) {
      b.setAttribute('aria-current', String(Number(b.dataset.n) === state.ch));
    });
  }

  /* ------------------------------------------------------------ chapter */
  function chapterUrl(n) {
    return R.chapterUrl + '?path=' + encodeURIComponent(path) + '&n=' + encodeURIComponent(n);
  }

  function renderChapter() {
    var page = el('rd-page');
    page.textContent = '';
    state.chapter.items.forEach(function (it, i) {
      if (it.type === 'img') {
        var img = document.createElement('img');
        img.loading = 'lazy';
        img.alt = '';
        img.src = R.imageUrl + '?path=' + encodeURIComponent(path) + '&src=' + encodeURIComponent(it.src);
        img.className = 'rd-item rd-img';
        img.dataset.i = i;
        page.appendChild(img);
        return;
      }
      var p = document.createElement('p');
      p.className = 'rd-item';
      p.dataset.i = i;
      p.textContent = it.text;
      page.appendChild(p);
    });
    state.items = state.chapter.items;
    markToc();
    renderPosition();
  }

  function goChapter(n, itemIndex, scroll) {
    var target = Number(n) || 1;
    if (state.meta) target = Math.max(1, Math.min(state.meta.total, target));
    return getJSON(chapterUrl(target)).then(function (ch) {
      state.chapter = ch;
      state.ch = ch.n;
      renderChapter();
      setCurrent(itemIndex || 0, scroll);
      if (scroll !== false) window.scrollTo({ top: 0, behavior: 'auto' });
      return ch;
    }).catch(function (e) {
      el('rd-page').innerHTML = '<p class="rd-item">' + esc(i18n('read.error')) + ' — ' + esc(e.message) + '</p>';
    });
  }

  function itemEls() {
    return Array.prototype.slice.call(document.querySelectorAll('#rd-page .rd-item'));
  }

  function setCurrent(i, scroll) {
    var els = itemEls();
    if (!els.length) return;
    i = Math.max(0, Math.min(els.length - 1, i));
    els.forEach(function (n) { n.classList.remove('current'); });
    var node = els[i];
    node.classList.add('current');
    state.cur = i;
    if (scroll) node.scrollIntoView({ behavior: 'smooth', block: 'center' });
    saveJSON(posKey, { ch: state.ch, i: i });
    renderPosition();
  }

  function renderPosition() {
    var total = state.meta ? state.meta.total : 1;
    var els = itemEls();
    el('rd-pos').textContent = fmt('read.position', { ch: state.ch, total: total });
    el('rd-p-pos').textContent = fmt('read.position_item', { i: state.cur + 1, total: els.length });
    var pct = ((state.ch - 1) + (els.length ? (state.cur + 1) / els.length : 0)) / total * 100;
    var bar = el('rd-progress-bar');
    if (bar) bar.style.width = Math.max(0, Math.min(100, pct)).toFixed(2) + '%';
  }

  function stepItem(delta) {
    var els = itemEls();
    if (!els.length) return;
    var i = state.cur + delta;
    if (i < 0) {
      if (state.chapter && state.chapter.prev) goChapter(state.chapter.prev, 1e9, true);
      return;
    }
    if (i >= els.length) {
      if (state.chapter && state.chapter.next) goChapter(state.chapter.next, 0, true);
      return;
    }
    setCurrent(i, true);
  }

  /* -------------------------------------------------------------- panels */
  function togglePanel(name) {
    var rd = el('rd');
    var open = rd.dataset.panel === name ? '' : name;
    rd.dataset.panel = open;
    ['toc', 'font', 'tts'].forEach(function (n) {
      var panel = el('rd-panel-' + n);
      var btn = el('rd-open-' + n);
      var show = n === open;
      if (panel) panel.hidden = !show;
      if (btn) btn.setAttribute('aria-pressed', String(show));
    });
    if (open === 'tts') warmVoices();
  }

  /* --------------------------------------------------------------- TTS */
  var tts = { state: 'unknown', voices: [], def: null, poll: 0, tries: 0 };

  function ttsStatus(text, cls) {
    var n = el('rd-tts-status');
    if (!n) return;
    n.textContent = text || '';
    n.className = 'muted small' + (cls ? ' ' + cls : '');
  }

  function warmVoices() {
    getJSON(R.voicesUrl).then(function (data) {
      tts.state = data.state;
      if (data.state === 'ready') {
        tts.voices = data.voices || [];
        tts.def = data.default;
        renderVoices();
        ttsStatus(fmt('read.tts_ready', { n: tts.voices.length, sec: data.load_seconds || 0 }));
        if (tts.poll) { clearTimeout(tts.poll); tts.poll = 0; }
      } else if (data.state === 'error') {
        ttsStatus(i18n('read.tts_error') + ' ' + (data.error || ''), 'error');
        if (tts.poll) { clearTimeout(tts.poll); tts.poll = 0; }
      } else {
        ttsStatus(i18n('read.tts_loading'));
        if (!tts.poll && tts.tries < 90) {
          tts.tries++;
          tts.poll = setTimeout(warmVoices, 2000);
        }
      }
    }).catch(function (e) {
      ttsStatus(i18n('read.tts_error') + ' ' + e.message, 'error');
    });
  }

  function renderVoices() {
    var sel = el('rd-tts-voice');
    sel.textContent = '';
    tts.voices.forEach(function (v) {
      var opt = document.createElement('option');
      opt.value = v.id;
      opt.textContent = v.label;
      if (v.desc) opt.title = v.desc;
      sel.appendChild(opt);
    });
    var want = ttsCfg.voice || tts.def;
    if (want && tts.voices.some(function (v) { return v.id === want; })) sel.value = want;
    ttsCfg.voice = sel.value;
    sel.disabled = !tts.voices.length;
    el('rd-tts-rate').value = ttsCfg.rate;
    el('rd-tts-rate-v').textContent = 'x' + Number(ttsCfg.rate).toFixed(2);
  }

  function ttsPost(text) {
    return fetch(R.ttsUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text, voice: ttsCfg.voice })
    }).then(function (res) {
      if (!res.ok) {
        return res.text().then(function (raw) {
          var data = null;
          try { data = JSON.parse(raw); } catch (e) { /* ignore */ }
          throw new Error((data && data.error) || ('HTTP ' + res.status));
        });
      }
      return res.blob();
    });
  }

  /* read-aloud session: one paragraph at a time, prefetching the next */
  var speak = { on: false, paused: false, i: 0, token: 0, audio: null, cache: {}, inflight: {} };

  function cacheKey(ch, i) { return ch + ':' + i; }
  function noop() {}

  function chapterItems(n) {
    if (state.chapter && state.chapter.n === n) return Promise.resolve(state.items);
    if (state.itemCache && state.itemCache.n === n) return Promise.resolve(state.itemCache.items);
    return getJSON(chapterUrl(n)).then(function (chap) {
      state.itemCache = { n: chap.n, items: chap.items };
      return chap.items;
    });
  }

  /* Audio blob for (chapter, item) — deduped, cached, one request in flight. */
  function audioFor(ch, i) {
    var k = cacheKey(ch, i);
    if (speak.cache[k]) return Promise.resolve(speak.cache[k]);
    if (speak.inflight[k]) return speak.inflight[k];

    var p = chapterItems(ch).then(function (items) {
      var it = items[i];
      if (!it || it.type !== 'p') return null;
      return ttsPost(it.text).then(function (blob) {
        var keys = Object.keys(speak.cache);
        if (keys.length > 6) delete speak.cache[keys[0]];
        speak.cache[k] = blob;
        return blob;
      });
    }).then(function (blob) {
      delete speak.inflight[k];
      return blob;
    }, function (e) {
      delete speak.inflight[k];
      throw e;
    });
    speak.inflight[k] = p;
    return p;
  }

  /* One-deep lookahead: VieNeu synthesizes at ~0.45x real time, so a single
     request in flight already keeps playback gapless — queueing two would just
     make the second wait behind the first on the server's synthesis lock. */
  function prefetchCurrent(i, count) {
    for (var k = 1; k <= count; k++) {
      if (i + k < state.items.length) audioFor(state.ch, i + k).catch(noop);
    }
    if (i + 1 >= state.items.length && state.chapter && state.chapter.next) {
      audioFor(state.chapter.next, 0).catch(noop);
    }
  }

  function speakNote(text) {
    var n = el('rd-tts-note');
    if (n) n.textContent = text || '';
  }

  function playBlob(blob, token) {
    if (token !== speak.token) return;
    var url = URL.createObjectURL(blob);
    if (speak.audio) {
      speak.audio.pause();
      try { URL.revokeObjectURL(speak.audio.src); } catch (e) { /* already revoked */ }
    }
    var audio = new Audio(url);
    speak.audio = audio;
    audio.playbackRate = ttsCfg.rate;
    audio.onended = function () {
      try { URL.revokeObjectURL(url); } catch (e) { /* already revoked */ }
      speakNext();
    };
    audio.onerror = function () { speakFail(i18n('read.tts_play_fail')); };
    audio.play().catch(function (e) { speakFail(e && e.message ? e.message : String(e)); });
  }

  function speakFail(message) {
    speakStop(false);
    speakNote(message);
    ttsStatus(message, 'error');
  }

  function speakStep() {
    if (!speak.on || speak.paused) return;
    var token = speak.token;
    var ch = state.ch;
    var i = speak.i;

    if (i >= state.items.length) {
      if (!state.chapter || !state.chapter.next) { speakDone(); return; }
      goChapter(state.chapter.next, 0, false).then(function () {
        if (token !== speak.token) return;
        speak.i = 0;
        speakStep();
      });
      return;
    }

    var it = state.items[i];
    if (!it || it.type !== 'p') { speak.i++; speakStep(); return; }

    setCurrent(i, true);
    markSpeaking();
    speakNote(fmt('read.tts_speaking', { ch: state.ch, i: i + 1, total: state.items.length }));
    ttsStatus(i18n('read.tts_synthesizing'));

    audioFor(ch, i).then(function (blob) {
      if (token !== speak.token) return;
      if (!blob) { speak.i++; speakStep(); return; }
      ttsStatus(i18n('read.tts_playing'));
      playBlob(blob, token);
      prefetchCurrent(i, 1);
    }).catch(function (e) {
      if (token !== speak.token) return;
      speakFail(e && e.message ? e.message : String(e));
    });
  }

  function speakNext() {
    if (!speak.on || speak.paused) return;
    speak.i++;
    speakStep();
  }

  function speakStart() {
    if (!state.chapter) return;
    speak.on = true;
    speak.paused = false;
    speak.token++;
    speak.i = state.cur;
    el('rd-tts-play').hidden = true;
    el('rd-tts-pause').hidden = false;
    el('rd-tts-stop').hidden = false;
    markSpeaking();
    speakStep();
  }

  function markSpeaking() {
    document.querySelectorAll('#rd-page .rd-item').forEach(function (n) { n.classList.remove('speaking'); });
    var cur = document.querySelector('#rd-page .rd-item.current');
    if (speak.on && cur) cur.classList.add('speaking');
  }

  function speakPause() {
    if (!speak.on) return;
    speak.paused = true;
    if (speak.audio) speak.audio.pause();
    el('rd-tts-pause').hidden = true;
    el('rd-tts-play').hidden = false;
    ttsStatus(i18n('read.tts_paused'));
    speakNote(i18n('read.tts_paused_note'));
  }

  function speakResume() {
    if (!speak.on) return;
    speak.paused = false;
    el('rd-tts-play').hidden = true;
    el('rd-tts-pause').hidden = false;
    if (speak.audio && speak.audio.paused && speak.audio.src) {
      ttsStatus(i18n('read.tts_playing'));
      speak.audio.play().catch(function () { speakStep(); });
    } else {
      speakStep();
    }
  }

  function speakStop(updateNote) {
    speak.on = false;
    speak.paused = false;
    speak.token++;
    if (speak.audio) {
      speak.audio.pause();
      try { URL.revokeObjectURL(speak.audio.src); } catch (e) { /* already revoked */ }
      speak.audio = null;
    }
    el('rd-tts-play').hidden = false;
    el('rd-tts-pause').hidden = true;
    el('rd-tts-stop').hidden = true;
    ttsStatus(tts.state === 'ready' ? fmt('read.tts_ready_n', { n: tts.voices.length }) : ttsStatusText());
    if (updateNote !== false) speakNote('');
    markSpeaking();
  }

  function ttsStatusText() {
    return tts.state === 'ready' ? '' : i18n('read.tts_loading');
  }

  function speakDone() {
    speakStop(false);
    speakNote(i18n('read.tts_finished'));
    ttsStatus(fmt('read.tts_ready_n', { n: tts.voices.length }));
  }

  function stopForNavigation() {
    if (speak.on) speakStop(true);
  }

  /* -------------------------------------------------------------- wiring */
  document.addEventListener('DOMContentLoaded', function () {
    var savedSet = loadJSON(setKey, null);
    if (savedSet) settings = Object.assign(settings, savedSet);
    var savedTts = loadJSON(ttsKey, null);
    if (savedTts) ttsCfg = Object.assign(ttsCfg, savedTts);
    applySettings();
    bindSettings();

    // Panels
    ['toc', 'font', 'tts'].forEach(function (n) {
      var btn = el('rd-open-' + n);
      if (btn) btn.addEventListener('click', function () { togglePanel(n); });
    });

    // Chapter nav
    el('rd-prev-ch').addEventListener('click', function () { stopForNavigation(); goChapter(state.ch - 1, 0, true); });
    el('rd-next-ch').addEventListener('click', function () { stopForNavigation(); goChapter(state.ch + 1, 0, true); });
    el('rd-chapter').addEventListener('change', function () {
      stopForNavigation();
      goChapter(Number(this.value), 0, true);
    });

    // Paragraph jump
    el('rd-prev-p').addEventListener('click', function () { stepItem(-1); });
    el('rd-next-p').addEventListener('click', function () { stepItem(1); });

    // Click a paragraph → make it current
    el('rd-page').addEventListener('click', function (ev) {
      var node = ev.target.closest('.rd-item');
      if (!node) return;
      stopForNavigation();
      setCurrent(Number(node.dataset.i) || 0, false);
    });

    // Keyboard: ←/→ chapters, PageUp/PageDown paragraphs
    document.addEventListener('keydown', function (ev) {
      var tag = (ev.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
      if (ev.key === 'ArrowLeft') { stopForNavigation(); goChapter(state.ch - 1, 0, true); }
      else if (ev.key === 'ArrowRight') { stopForNavigation(); goChapter(state.ch + 1, 0, true); }
      else if (ev.key === 'PageDown') { ev.preventDefault(); stepItem(1); }
      else if (ev.key === 'PageUp') { ev.preventDefault(); stepItem(-1); }
      else if (ev.key === 'Escape') { togglePanel(rdPanel()); }
    });

    // TTS controls
    el('rd-tts-rate').addEventListener('input', function () {
      ttsCfg.rate = Number(this.value);
      el('rd-tts-rate-v').textContent = 'x' + ttsCfg.rate.toFixed(2);
      if (speak.audio) speak.audio.playbackRate = ttsCfg.rate;
      saveJSON(ttsKey, ttsCfg);
    });
    el('rd-tts-voice').addEventListener('change', function () {
      ttsCfg.voice = this.value;
      saveJSON(ttsKey, ttsCfg);
    });
    el('rd-tts-play').addEventListener('click', function () {
      if (speak.on && speak.paused) { speakResume(); return; }
      if (!tts.voices.length) { warmVoices(); speakNote(i18n('read.tts_loading')); return; }
      speakStart();
    });
    el('rd-tts-pause').addEventListener('click', speakPause);
    el('rd-tts-stop').addEventListener('click', function () { speakStop(true); });
    el('rd-tts-test').addEventListener('click', function () {
      if (!tts.voices.length) { warmVoices(); return; }
      var text = i18n('read.test_text');
      ttsStatus(i18n('read.tts_synthesizing'));
      ttsPost(text).then(function (blob) {
        ttsStatus(i18n('read.tts_playing'));
        var audio = new Audio(URL.createObjectURL(blob));
        audio.playbackRate = ttsCfg.rate;
        audio.onended = function () { ttsStatus(fmt('read.tts_ready_n', { n: tts.voices.length })); };
        audio.play();
      }).catch(function (e) {
        ttsStatus(i18n('read.tts_error') + ' ' + e.message, 'error');
      });
    });

    // Boot: meta → saved position → first chapter
    el('rd-page').innerHTML = '<p class="rd-item muted">' + esc(i18n('read.loading')) + '</p>';
    getJSON(R.metaUrl + '?path=' + encodeURIComponent(path)).then(function (meta) {
      state.meta = meta;
      buildToc();
      var pos = loadJSON(posKey, { ch: 1, i: 0 }) || { ch: 1, i: 0 };
      return goChapter(pos.ch || 1, pos.i || 0, false);
    }).then(function () {
      var pos = loadJSON(posKey, null);
      if (pos && pos.i) setCurrent(pos.i, true);
    }).catch(function (e) {
      el('rd-page').innerHTML = '<p class="rd-item">' + esc(i18n('read.error')) + ' — ' + esc(e.message) + '</p>';
    });

    warmVoices();
  });

  function rdPanel() { return document.getElementById('rd').dataset.panel || 'toc'; }
})();
