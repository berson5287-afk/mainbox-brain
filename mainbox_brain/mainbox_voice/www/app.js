/* MaINbox Voice — PWA client logic.
 * Talks only to the Brain voice server over HTTP. Uses Chrome's Web Speech API
 * for STT/TTS.
 * v0.10: EVERYTHING you see is durable — the conversation, result rows, source
 * cards, the RFQ you're building, the Listen transcript, alerts and the tab you
 * were on all come back after a reload, a background kill, or a phone restart.
 * Saved on every change (debounced) and again the moment the app is hidden.   */
'use strict';

/* ---------- tiny helpers ---------- */
const $ = (id) => document.getElementById(id);
const el = (tag, cls, txt) => { const e = document.createElement(tag);
  if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const esc = (s) => (s == null ? '' : String(s));

/* ---------- settings (persisted) ---------- */
const DEFAULTS = {
  url: location.origin.startsWith('http') ? location.origin : '',
  token: '', speak: true, autoListen: false, wake: true, rate: 1.0, voice: ''
};
const S = Object.assign({}, DEFAULTS, load());
function load() { try { return JSON.parse(localStorage.getItem('mbb') || '{}'); }
  catch (e) { return {}; } }
function save() { try { localStorage.setItem('mbb', JSON.stringify(S)); }
  catch (e) {} }

/* ---------- v0.10: durable client state ---------- */
const LS = {
  get(k, d) { try { const v = localStorage.getItem(k);
    return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
};
const FEED = LS.get('mbb_feed', []);            // replayable transcript
const FEED_MAX = 150;
let replaying = false;
function feedPush(entry) {
  if (replaying) return;
  FEED.push(entry);
  if (FEED.length > FEED_MAX) FEED.splice(0, FEED.length - FEED_MAX);
  persistSoon();
}
const UI = Object.assign({ tab: 'chat', lastNotifId: 0, unseen: 0 },
                         LS.get('mbb_ui', {}));
let _persistTimer = null;
function persistSoon() { if (_persistTimer) return;
  _persistTimer = setTimeout(persistNow, 250); }
function persistNow() {
  if (_persistTimer) { clearTimeout(_persistTimer); _persistTimer = null; }
  // v0.10.3 (audit): cap the feed by BYTES, not entries -- big result/source
  // entries could brush the localStorage quota and silently kill every save.
  try {
    while (FEED.length > 5 && JSON.stringify(FEED).length > 2000000) FEED.splice(0, 10);
  } catch (e) {}
  LS.set('mbb_feed', FEED);
  LS.set('mbb_ui', UI);
  try { LS.set('mbb_listen', { buffer: listenBuffer || '' }); } catch (e) {}
  try { saveRfqDraft(); } catch (e) {}
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) persistNow(); else onResume(); });
window.addEventListener('pagehide', persistNow);
window.addEventListener('beforeunload', persistNow);

/* token can arrive in the URL (?token=...): capture then clean the URL */
(function grabToken() {
  const p = new URLSearchParams(location.search);
  const t = p.get('token');
  // v0.10.3 (audit): a token link from a NEW server must also update the base
  // URL, or every call 401s against the old server with the new token.
  if (t) { S.token = t; if (location.origin.startsWith('http')) S.url = location.origin; save();
    history.replaceState({}, '', location.pathname); }
})();

/* ---------- API ---------- */
function api(path, opts) {
  const base = (S.url || location.origin).replace(/\/+$/, '');
  const o = Object.assign({ headers: {} }, opts || {});
  o.headers['X-MBB-Token'] = S.token || '';
  if (o.body) o.headers['Content-Type'] = 'application/json';
  return fetch(base + path, o);
}
async function apiJSON(path, opts) { const r = await api(path, opts);
  if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }

/* ---------- connection status dot ---------- */
async function ping() {
  try { const d = await apiJSON('/api/ping');
    setConn(true); $('ver').textContent = 'Server v' + (d.version || '?') +
      ' · toolkit ' + (d.toolkit && d.toolkit.dispatch ? 'ready' : 'partial');
    return true;
  } catch (e) { setConn(false); return false; }
}
function setConn(ok) { const c = $('conn');
  c.className = 'dot ' + (ok ? 'ok' : 'bad'); }

/* ========================================================================
 * SPEECH: recognition (STT) + synthesis (TTS)
 * ====================================================================== */
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let recog = null, recognizing = false, recogMode = null; // 'ask' | 'listen'
const APP_VERSION = '0.10.3';
console.log('MaINbox Voice app v' + APP_VERSION);
let listenBuffer = '';
let listenSession = '';

/* v0.8.6: Android Chrome re-delivers the same growing utterance as "final"
   after its internal restarts, so naive `buffer += final` stacks repeats
   ("I'm | I'm going | I'm going to…"). Merge with word-overlap dedupe:
   append only the part of the new chunk that isn't already the tail. */
function mergeDedup(saved, chunk) {
  saved = (saved || '').trim(); chunk = (chunk || '').trim();
  if (!chunk) return saved;
  if (!saved) return chunk;
  const sl = saved.toLowerCase(), cl = chunk.toLowerCase();
  if (sl.endsWith(cl)) return saved;                // exact repeat (any case)
  const sw = sl.split(' '), cw = cl.split(' ');
  const cwOrig = chunk.split(' ');
  for (let k = Math.min(sw.length, cw.length); k > 0; k--) {
    if (sw.slice(-k).join(' ') === cw.slice(0, k).join(' '))
      return saved + ' ' + cwOrig.slice(k).join(' ');
  }
  return saved + ' ' + chunk;
}

function speechSupported() { return !!SR; }

function makeRecognizer(continuous) {
  const r = new SR();
  r.lang = 'en-US';
  r.interimResults = true;
  r.continuous = continuous;
  r.maxAlternatives = 1;
  return r;
}

/* ASK mic: one utterance -> send */
function startAsk() {
  if (!speechSupported()) return micUnsupported();
  stopSpeaking();
  recogMode = 'ask';
  recog = makeRecognizer(false);
  let finalText = '';
  recog.onstart = () => { recognizing = true; setMic(true);
    setStatus('Listening…', true); };
  recog.onresult = (e) => {
    let interim = '';
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const t = e.results[i][0].transcript;
      if (e.results[i].isFinal) finalText += t; else interim += t;
    }
    $('textIn').value = (finalText + interim).trim();
  };
  recog.onerror = (e) => { setStatus(recogErr(e.error)); endMic(); };
  recog.onend = () => { endMic();
    const t = $('textIn').value.trim();
    if (t) { $('textIn').value = ''; sendQuery(t); }
  };
  try { recog.start(); } catch (e) { setStatus('Mic busy'); endMic(); }
}

/* LISTEN mode: continuous, accumulate transcript */
/* v0.8.8: mic-busy handling — during a phone call Android's telephony stack
   owns the microphone, so recognition dies instantly. Instead of an
   error-spamming tight loop: detect the busy pattern (start→dead in <2s,
   or capture/permission errors), show the speakerphone tip ONCE, retry
   every 3s, and resume automatically the moment the mic frees. */
let listenFails = 0;
let lastListenStart = 0;
let listenRetryTimer = null;
let busyHintShown = false;
const BUSY_HINT = '🎤 Mic is busy — probably a phone call. Android never ' +
  'shares the caller\'s audio with apps; to capture a call, switch it to ' +
  'SPEAKERPHONE and keep this screen on. Retrying automatically…';

function startListen() {
  if (!speechSupported()) return micUnsupported();
  stopSpeaking();
  recogMode = 'listen';
  listenBuffer = '';
  listenSession = '';
  listenFails = 0;
  busyHintShown = false;
  if (listenRetryTimer) { clearTimeout(listenRetryTimer); listenRetryTimer = null; }
  $('liveText').textContent = '';
  recog = makeRecognizer(true);
  recog.onstart = () => { recognizing = true;
    lastListenStart = Date.now();
    $('bigListen').classList.add('on'); $('bigListen').innerHTML = 'Stop<br>Listening';
    $('extractBtn').style.display = 'none'; acquireWake();
    if ($('liveText').textContent === BUSY_HINT)
      $('liveText').textContent = 'Listening…';
  };
  recog.onresult = (e) => {
    listenFails = 0;                      // audio flowing — mic is ours
    // Rebuild THIS session from scratch every event, folding each result
    // through mergeDedup — on some Androids every result AGAIN contains the
    // utterance-from-the-start, so plain concatenation duplicates INSIDE a
    // single event.
    let sess = '';
    for (let i = 0; i < e.results.length; i++) {
      const t = (e.results[i][0].transcript || '').trim();
      if (t) sess = mergeDedup(sess, t);
    }
    listenSession = sess;
    $('liveText').textContent =
      mergeDedup(listenBuffer, listenSession) || 'Listening…';
  };
  recog.onerror = (e) => {
    if (e.error === 'audio-capture' || e.error === 'not-allowed'
        || e.error === 'service-not-allowed' || e.error === 'aborted') {
      listenFails++;                      // busy-mic signature; stay quiet
      return;
    }
    if (e.error !== 'no-speech')
      $('liveText').textContent += ' [' + recogErr(e.error) + ']';
  };
  recog.onend = () => {
    // commit this session into the saved transcript (dedup overlap)
    listenBuffer = mergeDedup(listenBuffer, listenSession);
    listenSession = '';
    persistSoon();                        // v0.10: transcript survives
    if (recogMode === 'listen' && recognizing) {
      const alive = Date.now() - lastListenStart;
      if (alive < 2000) listenFails++;
      if (listenFails >= 2) {
        // mic is held (phone call etc.) — back off, keep trying
        if (!busyHintShown) {
          busyHintShown = true;
          if (!listenBuffer.trim()) $('liveText').textContent = BUSY_HINT;
        }
        $('bigListen').innerHTML = 'Mic busy<br>retrying…';
        listenRetryTimer = setTimeout(() => {
          if (recogMode === 'listen' && recognizing) {
            try { lastListenStart = Date.now(); recog.start(); } catch (e2) {}
          }
        }, 3000);
        return;                            // wake lock stays held
      }
      try { lastListenStart = Date.now(); recog.start(); return; } catch (e2) {}
    }
    recognizing = false; $('bigListen').classList.remove('on');
    $('bigListen').innerHTML = 'Start<br>Listening'; releaseWake();
    if (listenBuffer.trim()) $('extractBtn').style.display = 'inline-block';
  };
  try { recog.start(); } catch (e) {}
}
function stopListen() { recognizing = false;
  if (listenRetryTimer) { clearTimeout(listenRetryTimer); listenRetryTimer = null; }
  if (recog) try { recog.stop(); } catch (e) {} }

function micUnsupported() {
  setStatus('Speech needs Chrome + secure context');
  addMsg('sys', 'Voice input needs Chrome and a secure connection. See Settings → the note about enabling the mic over Tailscale, or type your request below.');
}
function recogErr(code) {
  if (code === 'not-allowed' || code === 'service-not-allowed')
    return 'Mic blocked — allow microphone access';
  if (code === 'no-speech') return 'Didn\'t hear anything';
  if (code === 'audio-capture') return 'Mic busy — on a call? Use speakerphone';
  if (code === 'network') return 'Speech network error';
  return 'Mic: ' + code;
}

/* ---- TTS ---- */
let voices = [];
function loadVoices() { voices = window.speechSynthesis ?
  speechSynthesis.getVoices() : [];
  const sel = $('setVoice'); if (!sel) return;
  sel.innerHTML = '';
  const en = voices.filter((v) => /en/i.test(v.lang));
  (en.length ? en : voices).forEach((v) => {
    const o = el('option', null, v.name + ' (' + v.lang + ')');
    o.value = v.name; sel.appendChild(o);
  });
  if (S.voice) sel.value = S.voice;
}
function speak(text) {
  if (!S.speak || !text || !window.speechSynthesis) return;
  stopSpeaking();
  const u = new SpeechSynthesisUtterance(text);
  u.rate = S.rate || 1.0;
  const v = voices.find((x) => x.name === S.voice);
  if (v) u.voice = v;
  u.onend = () => { if (S.autoListen && document.getElementById('view-chat')
    .classList.contains('active')) setTimeout(startAsk, 250); };
  speechSynthesis.speak(u);
}
function stopSpeaking() { if (window.speechSynthesis) speechSynthesis.cancel(); }

/* ========================================================================
 * ASK flow
 * ====================================================================== */
async function sendQuery(text) {
  addMsg('heard', '“' + text + '”');
  setStatus('Thinking…');
  try {
    const d = await apiJSON('/api/query', { method: 'POST',
      body: JSON.stringify({ text }) });
    setConn(true);
    try {                                  // v0.10.3: render errors are not "offline"
      if (d.reply) addMsg('bot', d.reply);
      handleActionExtras(d);
      if (d.results && d.results.length) renderResults(d.results);
      if (d.sources && d.sources.length) renderSources(d.sources);
      if (d.events && d.events.length) renderFeedEvents(d.events);
      if (d.speak) speak(d.speak);
    } catch (re) { addMsg('sys', 'Display error: ' + re); }
    setStatus('Tap the mic and speak');
  } catch (e) {
    setConn(false);
    addMsg('bot', 'Could not reach the Brain server. Check Settings.');
    setStatus('Offline');
  }
}

/* ---------- v0.8.0: long-press, action sheet, draft editing ---------- */
let EDITING = null;                       // ref being edited in the RFQ form

function onLongPress(elm, fn) {
  let t = null;
  const start = (ev) => { t = setTimeout(() => { t = null; fn(ev); }, 550); };
  const cancel = () => { if (t) { clearTimeout(t); t = null; } };
  elm.addEventListener('touchstart', start, { passive: true });
  elm.addEventListener('touchend', cancel);
  elm.addEventListener('touchmove', cancel);
  elm.addEventListener('contextmenu', (ev) => { ev.preventDefault(); fn(ev); });
}

function showSheet(title, items) {        // items: [{label, danger?, fn}]
  const back = el('div', 'sheetback');
  const panel = el('div', 'sheet');
  panel.appendChild(el('div', 'sheettitle', title));
  items.forEach((it) => {
    const b = el('button', 'sheetbtn' + (it.danger ? ' danger' : ''), it.label);
    b.onclick = () => { back.remove(); it.fn && it.fn(); };
    panel.appendChild(b);
  });
  const cx = el('button', 'sheetbtn cancel', 'Cancel');
  cx.onclick = () => back.remove();
  panel.appendChild(cx);
  back.onclick = (ev) => { if (ev.target === back) back.remove(); };
  back.appendChild(panel);
  document.body.appendChild(back);
}

function enterEdit(p) {                   // p = preview payload from server
  EDITING = p.ref;
  $('rfqVendor').value = (p.to || []).join(', ');
  $('rfqJob').value = p.job || '';
  $('rfqNote').value = p.note || '';
  RFQ.lines = (p.lines || []).map((l) => ({ qty: l.qty, unit: l.unit || '',
    part: l.part, note: l.note || '' }));
  renderRfqLines();
  showEditChrome(p.ref);
  document.querySelector('[data-view="rfq"]').click();
}
function showEditChrome(ref) {             // v0.10: shared with draft restore
  EDITING = ref;
  $('rfqSend').textContent = 'Save changes to ' + ref;
  $('rfqResult').textContent = 'Editing ' + ref +
    ' — tap Save when done, or Cancel edit.';
  let cb = $('rfqCancelEdit');
  if (!cb) {
    cb = el('button', '', 'Cancel edit');
    cb.id = 'rfqCancelEdit';
    cb.style.cssText = 'margin-top:8px;background:#2a3140;color:#c8cdd8;' +
      'border:none;border-radius:8px;padding:8px 14px;font-size:14px';
    cb.onclick = exitEdit;
    $('rfqSend').parentNode.insertBefore(cb, $('rfqSend').nextSibling);
  }
  cb.style.display = 'inline-block';
  persistSoon();
}

function exitEdit() {
  EDITING = null;
  RFQ.lines = []; renderRfqLines();
  $('rfqVendor').value = ''; $('rfqJob').value = ''; $('rfqNote').value = '';
  $('rfqSend').textContent = 'Send RFQ';
  $('rfqResult').textContent = '';
  const cb = $('rfqCancelEdit'); if (cb) cb.style.display = 'none';
  persistSoon();
}

function handleActionExtras(d) {          // shared by typed + voice ask paths
  if (d && d.action === 'rfq_edit' && d.prefill) enterEdit(d.prefill);
  // v0.10: follow-up answers refresh the Follow-ups tab's cache
  if (d && d.followups) { FU.items = d.followups; FU.at = Date.now();
    LS.set('mbb_fu', FU); renderFollowups(); }
  if (d && d.action === 'followup' && d.followup_result && d.followup_result.ok)
    setTimeout(() => loadFollowups(true), 1500);
}

function addMsg(kind, text) {
  const f = $('feed');
  const m = el('div', 'msg ' + kind, text);
  f.appendChild(m); scrollFeed();
  if (kind !== 'sys') feedPush({ k: 'msg', c: kind, t: text });
  return m;
}

function renderFeedEvents(events) {
  const f = $('feed');
  events.forEach((e) => f.appendChild(eventCard(e)));
  scrollFeed();
  feedPush({ k: 'events', events });
}

/* v0.10: "where did that come from" — one tappable card per source email.
   Tap opens the email viewer (served by the voice server; it reads the live
   Outlook item on the PC) with a button to pop it open in Outlook there. */
function renderSources(srcs) {
  const f = $('feed');
  const wrap = el('div', 'srcwrap');
  wrap.appendChild(el('div', 'srchead', '📎 From ' + srcs.length + ' email' +
    (srcs.length > 1 ? 's' : '') + ' — tap to read'));
  const base = (S.url || location.origin).replace(/\/+$/, '');
  srcs.forEach((s) => {
    const c = el('div', 'src');
    const who = s.vendor || s.from_name || s.from || '?';
    c.appendChild(el('div', 'st', who +
      (s.roles && s.roles.length ? '  ·  ' + s.roles.join(', ') : '')));
    c.appendChild(el('div', 'ss', s.subject || '(no subject)'));
    c.appendChild(el('div', 'sm', [s.when, (s.from_name && s.from_name !== who)
      ? s.from_name : '', s.detail].filter(Boolean).join(' — ')));
    if (s.url) {
      c.onclick = () => window.open(base + s.url, '_blank', 'noopener');
      onLongPress(c, () => showSheet(s.subject || who, [
        { label: 'Read the email', fn: () => window.open(base + s.url, '_blank', 'noopener') },
        { label: 'Open in Outlook on the PC', fn: async () => {
          try { const d = await apiJSON('/api/mail/open', { method: 'POST',
            body: JSON.stringify({ key: s.key }) });
            addMsg('sys', d.ok ? '✓ Opened in Outlook on the PC' : '✗ ' + (d.error || 'failed'));
          } catch (e) { addMsg('sys', '✗ server unreachable'); } } },
        { label: 'Follow up on this email…', fn: () => {
          $('fuNote').value = 'Follow up: ' + (s.subject || who);
          $('fuLink').value = s.key || '';
          $('fuLinkLabel').textContent = '🔗 ' + (s.subject || who);
          $('fuLinkLabel').style.display = 'block';
          showTab('followups'); $('fuNote').focus(); } }
      ]));
    } else c.classList.add('dead');
    wrap.appendChild(c);
  });
  f.appendChild(wrap); scrollFeed();
  feedPush({ k: 'sources', sources: srcs });
}

/* v0.10: bring the conversation back exactly as it was */
function replayFeed() {
  replaying = true;
  FEED.forEach((e) => {
    try {                                  // v0.10.3: per-entry -- one bad entry must not drop the rest
      if (e.k === 'msg') addMsg(e.c, e.t);
      else if (e.k === 'results') renderResults(e.rows || []);
      else if (e.k === 'events') (e.events || []).forEach((ev) =>
        $('feed').appendChild(eventCard(ev)));
      else if (e.k === 'sources') renderSources(e.sources || []);
    } catch (err) { /* skip this entry */ }
  });
  replaying = false;
  scrollFeed();
}
function clearFeed() {
  FEED.splice(0, FEED.length); persistNow();
  $('feed').innerHTML = '';
  addMsg('sys', 'Conversation cleared.');
}
function scrollFeed() { const main = document.querySelector('main');
  main.scrollTop = main.scrollHeight; }

let lastResults = [];
function renderResults(rows) {
  lastResults = rows;
  feedPush({ k: 'results', rows });
  const f = $('feed');
  rows.forEach((r) => {
    const wrap = el('div', 'result' + (r.decided === 'confirmed' ? ' confirmed' :
      r.decided === 'rejected' ? ' rejected' : ''));
    wrap.dataset.n = r.n;
    const num = el('div', 'num', String(r.n));
    const body = el('div', 'body');
    const part = el('div', 'part');
    part.innerHTML = (r.equiv_mfr ? '<span class="brand">' + esc(r.equiv_mfr) +
      '</span> ' : '') + esc(r.equiv_part);
    if (r.decided === 'confirmed') part.innerHTML += '<span class="tag ok">confirmed</span>';
    if (r.decided === 'rejected') part.innerHTML += '<span class="tag no">rejected</span>';
    body.appendChild(part);
    if (r.meta) body.appendChild(el('div', 'meta', r.meta));
    // v0.8.2: tap an equivalent -> visit spec site / email the spec sheet
    body.style.cursor = 'pointer';
    body.onclick = () => {
      const item = ((r.equiv_mfr || '') + ' ' + r.equiv_part).trim();
      const url = r.url ||
        ('https://www.google.com/search?q=' +
         encodeURIComponent(item + ' spec sheet'));
      showSheet(item, [
        { label: 'Visit spec site', fn: () => window.open(url, '_blank') },
        { label: 'Email spec sheet', fn: () => {
          location.href = 'mailto:?subject=' +
            encodeURIComponent('Spec sheet — ' + item) + '&body=' +
            encodeURIComponent('As per your request, attached is the spec ' +
              'sheet for ' + item + '.\n\nSpec page: ' + url);
        } }
      ]);
    };
    const acts = el('div', 'acts');
    const ok = el('button', 'chip ok', '✓'); ok.title = 'Confirm';
    const no = el('button', 'chip no', '✗'); no.title = 'Reject';
    ok.onclick = () => correct('confirm', r.n);
    no.onclick = () => correct('reject', r.n);
    acts.appendChild(ok); acts.appendChild(no);
    wrap.appendChild(num); wrap.appendChild(body); wrap.appendChild(acts);
    f.appendChild(wrap);
  });
  scrollFeed();
}

async function correct(action, n) {
  try {
    const d = await apiJSON('/api/query', { method: 'POST',
      body: JSON.stringify({ text: action + ' ' + n }) });
    if (d.reply) addMsg('bot', d.reply);
    handleActionExtras(d);
    if (d.speak) speak(d.speak);
    if (d.results) updateResultTags(d.results);
  } catch (e) { addMsg('bot', 'Correction failed — offline?'); }
}
function updateResultTags(rows) {
  rows.forEach((r) => {
    const node = document.querySelector('.result[data-n="' + r.n + '"]');
    if (!node) return;
    node.classList.toggle('confirmed', r.decided === 'confirmed');
    node.classList.toggle('rejected', r.decided === 'rejected');
    const part = node.querySelector('.part');
    part.querySelectorAll('.tag').forEach((t) => t.remove());
    if (r.decided === 'confirmed') { const t = el('span', 'tag ok', 'confirmed'); part.appendChild(t); }
    if (r.decided === 'rejected') { const t = el('span', 'tag no', 'rejected'); part.appendChild(t); }
  });
}

/* ========================================================================
 * LISTEN → events
 * ====================================================================== */
async function extractEvents() {
  const text = listenBuffer.trim();
  if (!text) return;
  $('extractBtn').textContent = 'Finding…';
  try {
    const d = await apiJSON('/api/extract_events', { method: 'POST',
      body: JSON.stringify({ text }) });
    renderEvents(d.events || []);
    if (d.speak) speak(d.speak);
  } catch (e) { $('eventList').innerHTML =
    '<div class="empty">Could not reach the server.</div>'; }
  $('extractBtn').textContent = 'Find calendar events';
}
function eventCard(e) {
  const c = el('div', 'event');
  c.appendChild(el('div', 't', e.title));
  c.appendChild(el('div', 'when', e.start_h));
  if (e.source) c.appendChild(el('div', 'src', '“' + e.source + '”'));
  const row = el('div', 'row');
  const add = el('a', 'btn primary', 'Add to Google Calendar');
  add.href = e.gcal_url; add.target = '_blank'; add.rel = 'noopener';
  const ics = el('a', 'btn ghost', '.ics');
  const base = (S.url || location.origin).replace(/\/+$/, '');
  ics.href = base + '/api/ics?token=' + encodeURIComponent(S.token) +
    '&title=' + encodeURIComponent(e.title) +
    '&start=' + encodeURIComponent(e.start_iso) +
    '&end=' + encodeURIComponent(e.end_iso);
  row.appendChild(add); row.appendChild(ics);
  c.appendChild(row);
  return c;
}
function renderEvents(events) {
  const list = $('eventList');
  list.innerHTML = '';
  if (!events.length) { list.innerHTML =
    '<div class="empty">No dates or times found in what I heard.</div>'; return; }
  events.forEach((e) => list.appendChild(eventCard(e)));
}

/* ========================================================================
 * RFQ builder (schema 2: multi-vendor, preview, timeline)
 * ====================================================================== */
const RFQ = { lines: [], pendingUnit: '' };

/* v0.10: the RFQ you're building survives a reload / background kill */
function saveRfqDraft() {
  LS.set('mbb_rfq', {
    job: $('rfqJob').value, vendor: $('rfqVendor').value,
    note: $('rfqNote').value, lines: RFQ.lines, pendingUnit: RFQ.pendingUnit,
    editing: EDITING, lnQty: $('lnQty').value, lnPart: $('lnPart').value });
}
function restoreRfqDraft() {
  const d = LS.get('mbb_rfq', null);
  if (!d) return;
  $('rfqJob').value = d.job || ''; $('rfqVendor').value = d.vendor || '';
  $('rfqNote').value = d.note || ''; $('lnQty').value = d.lnQty || '';
  $('lnPart').value = d.lnPart || '';
  RFQ.lines = Array.isArray(d.lines) ? d.lines : [];
  RFQ.pendingUnit = d.pendingUnit || '';
  renderRfqLines();
  if (d.editing) showEditChrome(d.editing);
  if (RFQ.lines.length) $('lnStatus').textContent =
    RFQ.lines.length + ' line(s) restored.';
}
['rfqJob', 'rfqVendor', 'rfqNote', 'lnQty', 'lnPart'].forEach((id) =>
  $(id).addEventListener('input', persistSoon));

function vendorEmails() {
  return $('rfqVendor').value.split(/[,;\s]+/)
    .map((s) => s.trim()).filter((s) => s.indexOf('@') > 0);
}

function renderRfqLines() {
  persistSoon();
  const box = $('rfqLines');
  box.innerHTML = '';
  RFQ.lines.forEach((ln, i) => {
    const row = el('div', 'rfqline');
    const q = el('div', 'q', String(ln.qty) + (ln.unit ? ' ' + ln.unit : ''));
    const p = el('div', 'p', ln.part + (ln.note ? '  (' + ln.note + ')' : ''));
    const x = el('button', 'x', '✕');
    x.onclick = () => { RFQ.lines.splice(i, 1); renderRfqLines(); };
    row.appendChild(q); row.appendChild(p); row.appendChild(x);
    box.appendChild(row);
  });
}

function addLineFromFields() {
  const part = $('lnPart').value.trim();
  if (!part) { $('lnStatus').textContent = 'Enter or say a part first.'; return; }
  let qty = parseFloat($('lnQty').value);
  if (!isFinite(qty) || qty <= 0) qty = 1;
  RFQ.lines.push({ qty, unit: RFQ.pendingUnit || '', part, note: '' });
  RFQ.pendingUnit = '';
  $('lnQty').value = ''; $('lnPart').value = '';
  $('lnStatus').textContent = RFQ.lines.length + ' line(s) so far.';
  renderRfqLines();
}

/* say a line — server parses it, we FILL the boxes for review (no silent add) */
function startLineVoice() {
  if (!speechSupported()) { $('lnStatus').textContent =
    'Voice needs the mic enabled — you can type the line instead.'; return; }
  stopSpeaking();
  const r = makeRecognizer(false);
  let heard = '';
  r.onstart = () => { $('lnMic').classList.add('listening');
    $('lnStatus').textContent = 'Listening for a line…'; };
  r.onresult = (e) => {
    for (let i = e.resultIndex; i < e.results.length; i++)
      if (e.results[i].isFinal) heard += e.results[i][0].transcript;
    $('lnStatus').textContent = '“' + heard.trim() + '”';
  };
  r.onerror = (e) => { $('lnStatus').textContent = recogErr(e.error);
    $('lnMic').classList.remove('listening'); };
  r.onend = async () => {
    $('lnMic').classList.remove('listening');
    const t = heard.trim();
    if (!t) return;
    try {
      const d = await apiJSON('/api/rfq/parse_line', { method: 'POST',
        body: JSON.stringify({ text: t }) });
      if (d.part) {
        $('lnQty').value = d.qty || 1;
        $('lnPart').value = d.part;
        RFQ.pendingUnit = d.unit || '';
        $('lnStatus').textContent = 'Heard: ' + (d.qty || 1) +
          (d.unit ? ' ' + d.unit : '') + ' × ' + d.part +
          ' — edit if needed, then tap Add.';
      } else $('lnStatus').textContent = 'Didn\'t catch a part in that.';
    } catch (err) { $('lnStatus').textContent = 'Server unreachable.'; }
  };
  try { r.start(); } catch (e) { $('lnStatus').textContent = 'Mic busy.'; }
}

async function previewRFQ() {
  if (!RFQ.lines.length) {
    $('rfqResult').textContent = 'Add at least one line item.'; return; }
  try {
    const d = await apiJSON('/api/rfq/preview', { method: 'POST',
      body: JSON.stringify({
        job: $('rfqJob').value.trim(),
        vendors: vendorEmails(),
        note: $('rfqNote').value.trim(),
        lines: RFQ.lines
      }) });
    if (d.ok) {
      $('pvSubject').textContent = d.subject;
      $('pvBody').textContent = 'To: ' +
        (d.vendors.length ? d.vendors.join(', ') : '(add vendor emails)') +
        '\n\n' + d.body;
      $('rfqPreview').style.display = 'block';
      $('rfqResult').textContent = '';
    } else $('rfqResult').textContent = '✗ ' + (d.error || 'preview failed');
  } catch (e) { $('rfqResult').textContent = '✗ Could not reach the server.'; }
}

async function sendRFQ() {
  const vendors = vendorEmails();
  if (!vendors.length) {
    $('rfqResult').textContent = 'Enter at least one vendor email.'; return; }
  if (!RFQ.lines.length) {
    $('rfqResult').textContent = 'Add at least one line item.'; return; }
  $('rfqSend').disabled = true;
  $('rfqResult').textContent = EDITING ? 'Saving…' : 'Sending…';
  try {
    const payload = { job: $('rfqJob').value.trim(), vendors,
                      note: $('rfqNote').value.trim(), lines: RFQ.lines };
    let d;
    if (EDITING) {                       // v0.8.0: save changes to the draft
      payload.ref = EDITING;
      d = await apiJSON('/api/rfq/update', { method: 'POST',
        body: JSON.stringify(payload) });
      if (d.ok) {
        $('rfqResult').textContent = '✓ ' + d.ref + ' updated (still a draft)';
        speak('Draft updated.');
        exitEdit(); loadRfqMeta();
      } else $('rfqResult').textContent = '✗ ' + (d.error || 'update failed');
      $('rfqSend').disabled = false;
      return;
    }
    d = await apiJSON('/api/rfq', { method: 'POST',
      body: JSON.stringify(payload) });
    if (d.ok) {
      const ns = d.vendors.filter((v) => v.status === 'sent').length;
      const nq = d.vendors.length - ns;
      $('rfqResult').textContent = '✓ ' + d.ref + ' — ' +
        (ns ? ns + ' sent' : '') + (ns && nq ? ', ' : '') +
        (nq ? nq + ' queued for the Outlook PC' : '');
      speak('R F Q ' + (nq ? 'queued.' : 'sent.'));
      RFQ.lines = []; renderRfqLines();
      $('rfqJob').value = ''; $('rfqNote').value = '';
      $('rfqVendor').value = '';
      $('rfqPreview').style.display = 'none';
      loadRfqMeta();
    } else $('rfqResult').textContent = '✗ ' + (d.error || 'failed');
  } catch (e) { $('rfqResult').textContent = '✗ Could not reach the server.'; }
  $('rfqSend').disabled = false;
}

/* recent vendors -> tappable chips that toggle into the input */
function renderVendorChips(vendors) {
  const box = $('vendorChips');
  box.innerHTML = '';
  (vendors || []).forEach((v) => {
    const c = el('button', 'vchip', v);
    const sync = () => c.classList.toggle('on',
      vendorEmails().map((x) => x.toLowerCase()).indexOf(v.toLowerCase()) >= 0);
    c.onclick = () => {
      let cur = vendorEmails();
      const i = cur.map((x) => x.toLowerCase()).indexOf(v.toLowerCase());
      if (i >= 0) cur.splice(i, 1); else cur.push(v);
      $('rfqVendor').value = cur.join(', ');
      sync();
    };
    sync();
    box.appendChild(c);
  });
}

const EV_CLS = { sent: '', replied: '', created: '', queued: 'q',
  send_failed: 'f', note: 'n', awarded: '', po_sent: '', delivered: '',
  closed: '' };

async function postEvent(ref, event, vendor, wrap, card) {
  try {
    const d = await apiJSON('/api/rfq/event', { method: 'POST',
      body: JSON.stringify({ ref, event, vendor: vendor || '' }) });
    if (d.ok) {
      wrap.replaceWith(expandedNode(d.rfq, card));
      loadRfqMeta();               // refresh list badges
    }
  } catch (e) {}
}

function expandedNode(rfq, card) {
  const wrap = el('div', 'tlwrap');
  // vendors with one-tap Replied
  (rfq.vendors || []).forEach((v) => {
    const row = el('div', 'tlrow');
    row.appendChild(el('span', 'tev ' +
      (v.status === 'replied' ? '' : v.status === 'queued' ? 'q' : ''),
      v.status));
    row.appendChild(el('span', 'tdet', v.email));
    if (v.status === 'sent') {
      const b = el('button', 'chip ok', 'replied');
      b.style.fontSize = '11px'; b.style.padding = '3px 8px';
      b.onclick = () => postEvent(rfq.ref, 'replied', v.email, wrap, card);
      row.appendChild(b);
    }
    wrap.appendChild(row);
  });
  // attention flags
  (rfq.attention || []).forEach((f) => {
    const row = el('div', 'tlrow');
    row.appendChild(el('span', 'tev f', '⚠'));
    row.appendChild(el('span', 'tdet', f));
    wrap.appendChild(row);
  });
  // timeline
  const tl = el('div', 'tl');
  (rfq.timeline || []).forEach((e) => {
    const row = el('div', 'tlrow');
    row.appendChild(el('span', 'tev ' + (EV_CLS[e.event] || ''), e.event));
    row.appendChild(el('span', 'tdet',
      (e.vendor ? e.vendor + ' — ' : '') + (e.detail || '')));
    const d = new Date((e.ts || 0) * 1000);
    row.appendChild(el('span', 'twhen',
      d.toLocaleDateString([], { month: 'numeric', day: 'numeric' }) + ' ' +
      d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })));
    tl.appendChild(row);
  });
  // lifecycle actions (manual now; automation appends the same events later)
  const acts = el('div', 'noterow');
  let sel = null;
  if ((rfq.vendors || []).length > 1) {
    sel = document.createElement('select');
    sel.style.cssText = 'background:var(--panel);color:var(--text);' +
      'border:1px solid var(--edge);border-radius:9px;padding:7px;flex:1';
    (rfq.vendors || []).forEach((v) => {
      const o = el('option', null, v.email); o.value = v.email;
      sel.appendChild(o); });
    acts.appendChild(sel);
  }
  const stageBtn = (label, ev) => {
    const b = el('button', 'chip ok', label);
    b.style.fontSize = '12px';
    b.onclick = () => postEvent(rfq.ref, ev,
      sel ? sel.value : ((rfq.vendors || [])[0] || {}).email, wrap, card);
    return b;
  };
  const already = new Set((rfq.timeline || []).map((e) => e.event));
  if (!already.has('awarded')) acts.appendChild(stageBtn('Awarded', 'awarded'));
  else if (!already.has('po_sent'))
    acts.appendChild(stageBtn('PO sent', 'po_sent'));
  else if (!already.has('delivered'))
    acts.appendChild(stageBtn('Delivered', 'delivered'));
  else if (!already.has('closed'))
    acts.appendChild(stageBtn('Closed', 'closed'));
  tl.appendChild(acts);
  // note input
  const nr = el('div', 'noterow');
  const inp = el('input');
  inp.placeholder = 'Add internal note (never emailed)…';
  const btn = el('button', 'chip ok', '+');
  btn.onclick = async () => {
    const t = inp.value.trim();
    if (!t) return;
    try {
      const d = await apiJSON('/api/rfq/note', { method: 'POST',
        body: JSON.stringify({ ref: rfq.ref, text: t }) });
      if (d.ok) { rfq.timeline = d.timeline;
        wrap.replaceWith(expandedNode(rfq, card)); }
    } catch (e) {}
  };
  nr.appendChild(inp); nr.appendChild(btn);
  tl.appendChild(nr);
  wrap.appendChild(tl);
  return wrap;
}

async function toggleRfqCard(card, ref) {
  const open = card.querySelector('.tlwrap');
  if (open) { open.remove(); return; }
  try {
    const d = await apiJSON('/api/rfq/get?ref=' + encodeURIComponent(ref));
    card.appendChild(expandedNode(d.rfq, card));
  } catch (e) {}
}

async function loadRfqMeta() {
  try {
    const d = await apiJSON('/api/rfq/list');
    const dl = $('vendorList'); dl.innerHTML = '';
    (d.vendors || []).forEach((v) => { const o = el('option'); o.value = v;
      dl.appendChild(o); });
    renderVendorChips(d.vendors);
    const box = $('rfqRecent'); box.innerHTML = '';
    const live = d.rfqs || [];
    const arch = [...(d.hidden || []).map((r) => ({ ...r, _kind: 'hidden' })),
                  ...(d.deleted || []).map((r) => ({ ...r, _kind: 'deleted' }))];
    if (!live.length && !arch.length) { box.innerHTML =
      '<div class="empty" style="padding:16px">None yet.</div>'; return; }

    const post = (path, ref) => apiJSON(path, { method: 'POST',
      body: JSON.stringify({ ref }) }).catch(() => {});

    async function previewInCard(card, ref) {
      let pv = card.querySelector('.pvblock');
      if (pv) { pv.remove(); return; }
      try {
        const p = await apiJSON('/api/rfq/preview', { method: 'POST',
          body: JSON.stringify({ ref }) });
        if (!p.ok) return;
        pv = el('div', 'pvblock', 'To: ' + p.to.join(', ') +
          '\nSubject: ' + p.subject + '\n\n' + p.body);
        card.appendChild(pv);
      } catch (e) { /* server unreachable */ }
    }

    live.forEach((r) => {
      const c2 = el('div', 'rfqcard');
      const stTxt = r.status === 'partial' && r.stage === 'partial'
        ? r.n_sent + '/' + r.n_vendors + ' sent' : r.stage;
      const stCls = r.stage === 'draft' ? 'draft'
        : (r.stage === 'queued' || r.stage === 'partial') ? 'queued' : 'sent';
      const st = el('span', 'rs ' + stCls, stTxt);
      if (r.health === 'bad') st.style.background = '#7a2020';
      c2.appendChild(st);
      c2.appendChild(el('span', 'rr', r.ref));
      const dx = el('button', 'rxdel', '✕');
      dx.title = 'Delete (can restore later)';
      dx.onclick = async (ev) => { ev.stopPropagation();
        await post('/api/rfq/delete', r.ref); loadRfqMeta(); };
      c2.appendChild(dx);
      c2.appendChild(el('div', 'rm', (r.job ? r.job + ' — ' : '') +
        r.n_vendors + ' vendor(s) — ' + r.n_lines + ' line(s) — ' +
        (r.created || '') + ' — tap for history, hold for actions'));
      (r.attention || []).forEach((f) => {
        const w = el('div', 'rm', '⚠ ' + f);
        w.style.color = r.health === 'bad' ? 'var(--red)' : 'var(--amber)';
        c2.appendChild(w);
      });
      if (r.stage === 'draft') {
        const sb = el('button', 'rsend', 'Send');
        sb.onclick = async (ev) => { ev.stopPropagation();
          sb.disabled = true; sb.textContent = 'Sending…';
          await post('/api/rfq/send', r.ref); loadRfqMeta(); };
        c2.appendChild(sb);
      }
      c2.style.cursor = 'pointer';
      c2.onclick = (ev) => {
        if (ev.target.tagName === 'INPUT' || ev.target.tagName === 'BUTTON'
            || ev.target.tagName === 'SELECT' || ev.target.tagName === 'OPTION')
          return;
        toggleRfqCard(c2, r.ref);
      };
      // v0.8.0: click-and-hold → action sheet
      onLongPress(c2, () => {
        const items = [
          { label: 'Preview email', fn: () => previewInCard(c2, r.ref) }];
        if (r.stage === 'draft') {
          items.push({ label: 'Edit draft', fn: async () => {
            try {
              const p = await apiJSON('/api/rfq/preview', { method: 'POST',
                body: JSON.stringify({ ref: r.ref }) });
              if (p.ok) enterEdit(p);
            } catch (e) { /* offline */ }
          } });
          items.push({ label: 'Send now', fn: async () => {
            await post('/api/rfq/send', r.ref); loadRfqMeta(); } });
        }
        items.push({ label: 'Hide', fn: async () => {
          await post('/api/rfq/hide', r.ref); loadRfqMeta(); } });
        items.push({ label: 'Delete', danger: true, fn: async () => {
          await post('/api/rfq/delete', r.ref); loadRfqMeta(); } });
        showSheet(r.ref, items);
      });
      box.appendChild(c2);
    });

    if (arch.length) {
      const hd = el('div', 'delhead',
                    'Hidden & deleted (' + arch.length + ') ▸');
      const wrap = el('div'); wrap.style.display = 'none';
      hd.onclick = () => { const open = wrap.style.display !== 'none';
        wrap.style.display = open ? 'none' : 'block';
        hd.textContent = 'Hidden & deleted (' + arch.length + ')'
          + (open ? ' ▸' : ' ▾'); };
      arch.forEach((r) => {
        const c2 = el('div', 'rfqcard del');
        c2.appendChild(el('span', 'rs deleted', r._kind));
        c2.appendChild(el('span', 'rr', r.ref));
        const rb = el('button', 'rsend',
                      r._kind === 'hidden' ? 'Unhide' : 'Restore');
        rb.onclick = async () => {
          await post(r._kind === 'hidden' ? '/api/rfq/unhide'
                                          : '/api/rfq/restore', r.ref);
          loadRfqMeta(); };
        c2.appendChild(rb);
        c2.appendChild(el('div', 'rm', r.n_vendors + ' vendor(s) — ' +
          r.n_lines + ' line(s) — ' + (r.created || '')));
        wrap.appendChild(c2);
      });
      box.appendChild(hd); box.appendChild(wrap);
    }
  } catch (e) { /* offline */ }
}

/* ========================================================================
 * NOTIFICATIONS (poll)
 * ====================================================================== */
let unseen = UI.unseen || 0;
let notifsPrimed = false;
async function pollNotifs() {
  if (document.hidden && notifsPrimed) return;   // v0.10: nothing to do in the background
  try {
    // v0.10: the FIRST poll after a (re)load rebuilds the list from the server
    // without re-firing an OS notification for every old alert
    const d = await apiJSON('/api/notifications?since=' +
      (notifsPrimed ? UI.lastNotifId : 0));
    const fresh = [];
    (d.notifications || []).forEach((n) => {
      if (!notifsPrimed) { addNotifToList(n); if (n.id > UI.lastNotifId) fresh.push(n); return; }
      addNotifToList(n); fresh.push(n);
    });
    fresh.forEach((n) => {
      UI.lastNotifId = Math.max(UI.lastNotifId, n.id);
      raiseNotification(n);
      if (!document.getElementById('view-notifs').classList.contains('active')) {
        unseen++; UI.unseen = unseen; updateBadge();
      }
      if (n.kind === 'followup') loadFollowups(true);
    });
    if (typeof d.latest === 'number' && !notifsPrimed)
      UI.lastNotifId = Math.max(UI.lastNotifId, fresh.length ? UI.lastNotifId : d.latest);
    notifsPrimed = true;
    persistSoon();
    setConn(true);
  } catch (e) { /* offline; leave dot */ }
}
function addNotifToList(n) {
  const list = $('notifList');
  if (list.querySelector('.empty')) list.innerHTML = '';
  const c = el('div', 'notif');
  c.dataset.id = n.id;
  const x = el('button', 'nx', '✕');
  x.title = 'Dismiss';
  x.onclick = async () => {
    try { await apiJSON('/api/notifications/dismiss', { method: 'POST',
      body: JSON.stringify({ id: n.id }) }); } catch (e) {}
    c.remove();
    if (!list.querySelector('.notif')) list.innerHTML =
      '<div class="empty">No alerts yet.<br>MaINbox can push RFQ and reminder alerts here.</div>';
  };
  c.appendChild(x);
  // v0.8.0: click-and-hold an alert → big Clear button
  onLongPress(c, () => {
    let big = c.querySelector('.clearbig');
    if (big) { big.remove(); return; }
    big = el('button', 'clearbig', 'Clear this alert');
    big.onclick = () => x.onclick();
    c.appendChild(big);
  });
  c.appendChild(el('div', 'nt', n.title));
  if (n.body) c.appendChild(el('div', 'nb', n.body));
  const dt = new Date((n.ts || 0) * 1000);
  c.appendChild(el('div', 'ntime', dt.toLocaleString()));
  // v0.10: tap an alert to go where it points
  if (n.kind === 'followup' || n.url) {
    c.style.cursor = 'pointer';
    c.onclick = (ev) => {
      if (ev.target.tagName === 'BUTTON') return;
      if (n.url) { const base = (S.url || location.origin).replace(/\/+$/, '');
        window.open((n.url.startsWith('http') ? '' : base) + n.url, '_blank', 'noopener'); }
      else showTab('followups');
    };
  }
  if (list.querySelector('[data-id="' + n.id + '"]')) return;   // already shown
  list.insertBefore(c, list.firstChild);
}
function raiseNotification(n) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  try { new Notification(n.title, { body: n.body || '', icon: 'icon-192.png' }); }
  catch (e) {}
}
(function stampVersion() {
  try {
    const v = document.getElementById('view-settings');
    if (v) {
      const d = document.createElement('div');
      d.textContent = 'MaINbox Voice app v' + APP_VERSION;
      d.style.cssText = 'margin-top:18px;color:#5a6377;font-size:12px;text-align:center';
      v.appendChild(d);
    }
  } catch (e) {}
})();

function updateBadge() { const b = $('notifBadge');
  if (unseen > 0) { b.style.display = 'inline-block'; b.textContent = unseen; }
  else b.style.display = 'none'; }
const _nc = document.getElementById('notifClear');
if (_nc) _nc.onclick = async () => {
  try { await apiJSON('/api/notifications/clear', { method: 'POST',
    body: JSON.stringify({}) }); } catch (e) {}
  $('notifList').innerHTML =
    '<div class="empty">No alerts yet.<br>MaINbox can push RFQ and reminder alerts here.</div>';
  unseen = 0; UI.unseen = 0; updateBadge(); persistSoon();
};

/* ========================================================================
 * WAKE LOCK
 * ====================================================================== */
let wakeLock = null;
async function acquireWake() {
  if (!S.wake || !('wakeLock' in navigator)) return;
  try { wakeLock = await navigator.wakeLock.request('screen'); } catch (e) {}
}
function releaseWake() { if (wakeLock) { try { wakeLock.release(); } catch (e) {} wakeLock = null; } }

/* ========================================================================
 * UI wiring
 * ====================================================================== */
function setMic(on) { $('micBtn').classList.toggle('listening', on); }
function endMic() { recognizing = false; setMic(false); }
function setStatus(t, live) { const s = $('status'); s.textContent = t;
  s.classList.toggle('live', !!live); }

function showTab(name) {
  const b = document.querySelector('.tabs button[data-view="' + name + '"]');
  if (!b) return;
  document.querySelectorAll('.tabs button').forEach((x) => x.classList.remove('active'));
  document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
  b.classList.add('active');
  $('view-' + name).classList.add('active');
  // hide mic bar outside Ask (RFQ has its own line mic)
  $('micbar').style.display = (name === 'chat') ? 'block' : 'none';
  if (name === 'notifs') { unseen = 0; UI.unseen = 0; updateBadge(); }
  if (name === 'rfq') loadRfqMeta();
  if (name === 'followups') loadFollowups(true);
  UI.tab = name; persistSoon();
}
document.querySelectorAll('.tabs button').forEach((b) => {
  b.onclick = () => showTab(b.dataset.view);
});

/* ========================================================================
 * v0.10: FOLLOW-UPS — a live mirror of MaINbox's follow-up queue.
 * Reads the snapshot the desktop publishes; every edit goes back through
 * MaINbox itself (its bridge applies it on the app's main thread), so the
 * desktop stays the single source of truth. Works offline from the last
 * synced copy; edits made while MaINbox is closed are queued and applied
 * when it next opens.
 * ====================================================================== */
let FU = Object.assign({ items: [], online: false, ver: '', at: 0 },
                       LS.get('mbb_fu', {}));

async function loadFollowups(silent) {
  try {
    const d = await apiJSON('/api/followups');
    FU = { items: d.items || [], online: !!d.mainbox_online,
           ver: d.mainbox_version || '', at: Date.now() };
    LS.set('mbb_fu', FU);
    setConn(true);
  } catch (e) { if (!silent) fuMsg('Could not reach the server — showing the last synced list.'); }
  renderFollowups();
}
function fuMsg(t, ok) { const m = $('fuMsg'); m.textContent = t || '';
  m.style.color = ok ? 'var(--green)' : 'var(--muted)'; }

function fuDueClass(it) {
  if (it.overdue) return 'over';
  try { const d = new Date(it.due);
    if (d - Date.now() < 3 * 3600 * 1000) return 'soon'; } catch (e) {}
  return '';
}

function renderFollowups() {
  const list = $('fuList'); list.innerHTML = '';
  const items = (FU.items || []).filter((i) =>
    !i.status || i.status === 'open' || i.status === 'Active');
  const over = items.filter((i) => i.overdue).length;
  const st = $('fuStatus');
  st.textContent = (FU.online ? '● MaINbox online' + (FU.ver ? ' v' + FU.ver : '')
    : '○ MaINbox offline — last synced ' +
      (FU.at ? new Date(FU.at).toLocaleString() : 'never')) +
    ' · ' + items.length + ' open' + (over ? ' · ' + over + ' overdue' : '');
  st.className = 'fustatus ' + (FU.online ? 'on' : 'off');
  const badge = $('fuBadge');
  if (over) { badge.style.display = 'inline-block'; badge.textContent = over; }
  else badge.style.display = 'none';
  if (!items.length) { list.innerHTML =
    '<div class="empty">No open follow-ups.' + (FU.online ? '' :
      '<br>(MaINbox isn\'t running — this is the last synced list.)') + '</div>';
    return; }
  items.forEach((it) => {
    const c = el('div', 'fu ' + fuDueClass(it));
    const top = el('div', 'futop');
    top.appendChild(el('span', 'fudue', it.due_display || ''));
    top.appendChild(el('span', 'fukind', it.kind_label || it.kind || ''));
    c.appendChild(top);
    c.appendChild(el('div', 'fusub', it.subject || it.note || '(no subject)'));
    const who = it.vendor || it.group || '';
    const meta = [who, (it.note && it.note !== it.subject) ? it.note : '']
      .filter(Boolean).join(' — ');
    if (meta) c.appendChild(el('div', 'fumeta', meta));
    c.onclick = () => fuSheet(it);
    list.appendChild(c);
  });
}

function fuSheet(it) {
  const title = (it.due_display ? it.due_display + ' — ' : '') + (it.subject || it.note || '');
  const items = [
    { label: 'Snooze 1 hour', fn: () => fuCmd('snooze', { id: it.id, due_at: 'in 1 hour', label: '1 hour' }) },
    { label: 'Snooze until tomorrow 9 AM', fn: () => fuCmd('snooze', { id: it.id, due_at: 'tomorrow 9am', label: 'Tomorrow 9 AM' }) },
    { label: 'Snooze… (pick a time)', fn: () => fuPick(it) },
    { label: '✓ Mark complete', fn: () => fuCmd('complete', { id: it.id }) },
    { label: 'Edit note…', fn: () => {
      const t = prompt('Follow-up note', it.note || '');
      if (t != null) fuCmd('note', { id: it.id, note: t }); } }
  ];
  if (it.entry_id) {
    const base = (S.url || location.origin).replace(/\/+$/, '');
    items.unshift({ label: '✉ Read the email', fn: () => window.open(base +
      '/api/mail/view?key=' + encodeURIComponent(it.entry_id) +
      '&token=' + encodeURIComponent(S.token), '_blank', 'noopener') });
  }
  items.push({ label: 'Cancel follow-up', danger: true, fn: () => {
    if (confirm('Cancel this follow-up in MaINbox?'))
      fuCmd('cancel', { id: it.id, reason: 'Cancelled from phone' }); } });
  showSheet(title, items);
}

function fuPick(it) {
  const box = $('fuPickBox');
  box.style.display = 'block';
  box.dataset.id = it.id;
  $('fuPickTitle').textContent = 'Snooze: ' + (it.subject || it.note || '');
  $('fuPickWhen').value = '';
  box.scrollIntoView({ behavior: 'smooth' });
}

async function fuCmd(op, args) {
  fuMsg((op === 'create' ? 'Creating' : 'Sending to MaINbox') + '…');
  try {
    const d = await apiJSON('/api/followups/cmd', { method: 'POST',
      body: JSON.stringify(Object.assign({ op }, args)) });
    if (d.ok && !d.queued) {
      fuMsg('✓ ' + (d.message || 'Done'), true);
      if (op === 'create') fuClearForm();
      setTimeout(() => loadFollowups(true), 800);   // desktop republishes at once
    } else if (d.queued) {
      fuMsg('⏳ ' + (d.message || 'Queued for MaINbox.'));
      if (op === 'create') fuClearForm();
    } else fuMsg('✗ ' + (d.error || 'failed'));
  } catch (e) { fuMsg('✗ Could not reach the server.'); }
}

function fuClearForm() {
  $('fuNote').value = ''; $('fuWhen').value = ''; $('fuLink').value = '';
  $('fuLinkLabel').style.display = 'none';
  LS.set('mbb_fu_draft', null);
}
function fuSaveDraft() {
  LS.set('mbb_fu_draft', { note: $('fuNote').value, when: $('fuWhen').value,
    link: $('fuLink').value, linkLabel: $('fuLinkLabel').textContent });
}
function fuRestoreDraft() {
  const d = LS.get('mbb_fu_draft', null);
  if (!d) return;
  $('fuNote').value = d.note || ''; $('fuWhen').value = d.when || '';
  $('fuLink').value = d.link || '';
  if (d.link) { $('fuLinkLabel').textContent = d.linkLabel || '🔗 linked email';
    $('fuLinkLabel').style.display = 'block'; }
}
['fuNote', 'fuWhen'].forEach((id) => $(id).addEventListener('input', fuSaveDraft));
$('fuCreate').onclick = () => {
  const note = $('fuNote').value.trim();
  const when = $('fuWhen').value.trim();
  if (!note) { fuMsg('Say what to follow up on.'); return; }
  if (!when) { fuMsg('Pick or type when (e.g. "tomorrow 9am", "friday 3pm").'); return; }
  const args = { note, due_at: when, label: when };
  if ($('fuLink').value) { args.entry_id = $('fuLink').value;
    args.subject = ($('fuLinkLabel').textContent || '').replace(/^🔗\s*/, ''); }
  fuCmd('create', args);
};
document.querySelectorAll('.fuq').forEach((b) => { b.onclick = () => {
  $('fuWhen').value = b.dataset.when; fuSaveDraft(); }; });
$('fuLinkClear').onclick = () => { $('fuLink').value = '';
  $('fuLinkLabel').style.display = 'none'; fuSaveDraft(); };
$('fuRefresh').onclick = () => loadFollowups(false);
$('fuPickGo').onclick = () => {
  const when = $('fuPickWhen').value.trim();
  if (!when) return;
  fuCmd('snooze', { id: $('fuPickBox').dataset.id, due_at: when, label: when });
  $('fuPickBox').style.display = 'none';
};
$('fuPickCancel').onclick = () => { $('fuPickBox').style.display = 'none'; };
$('fuMic').onclick = () => {
  if (!speechSupported()) return micUnsupported();
  const r = makeRecognizer(false);
  let heard = '';
  r.onstart = () => { $('fuMic').classList.add('listening'); fuMsg('Listening…'); };
  r.onresult = (e) => { for (let i = e.resultIndex; i < e.results.length; i++)
    if (e.results[i].isFinal) heard += e.results[i][0].transcript; };
  r.onerror = (e) => { fuMsg(recogErr(e.error)); $('fuMic').classList.remove('listening'); };
  r.onend = () => { $('fuMic').classList.remove('listening');
    const t = heard.trim(); if (!t) return;
    // "call george about the panel tomorrow at 9" -> note + when
    $('fuNote').value = t; fuMsg('Heard: "' + t + '" — pick when, or type it.');
    fuSaveDraft(); };
  try { r.start(); } catch (e) { fuMsg('Mic busy.'); }
};

$('lnAdd').onclick = addLineFromFields;
$('lnPart').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') addLineFromFields(); });
$('lnPart').addEventListener('input', () => { RFQ.pendingUnit = ''; });
$('lnMic').onclick = startLineVoice;
$('rfqSend').onclick = sendRFQ;
$('rfqPreviewBtn').onclick = previewRFQ;
$('pvClose').onclick = () => { $('rfqPreview').style.display = 'none'; };

$('micBtn').onclick = () => { if (recognizing) { try { recog.stop(); } catch (e) {} }
  else startAsk(); };
$('textIn').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { const t = $('textIn').value.trim();
    if (t) { $('textIn').value = ''; sendQuery(t); } } });
$('btnStop').onclick = stopSpeaking;
$('bigListen').onclick = () => { if (recognizing && recogMode === 'listen') stopListen();
  else startListen(); };
$('extractBtn').onclick = extractEvents;

/* v0.8.9: bring a Samsung Call Transcript (or any pasted text) into the
   Listen flow so "Find calendar events" works on recorded calls too. */
$('loadCallBtn').onclick = async () => {
  $('loadCallBtn').textContent = 'Loading…';
  try {
    const d = await apiJSON('/api/call_transcript/latest', {});
    if (d.ok && d.text) {
      listenBuffer = d.text; listenSession = '';
      $('liveText').textContent = d.text;
      $('extractBtn').style.display = 'inline-block';
    } else {
      $('liveText').textContent = 'No call transcripts yet — start the ' +
        'ingest watcher on the PC (CALL_INGEST.bat).';
    }
  } catch (e) { $('liveText').textContent = 'Could not reach the server.'; }
  $('loadCallBtn').textContent = 'Load last call';
};

$('pasteBtn').onclick = () => {
  const show = $('pasteBox').style.display === 'none';
  $('pasteBox').style.display = show ? 'block' : 'none';
  $('pasteUse').style.display = show ? 'inline-block' : 'none';
  if (show) $('pasteBox').focus();
};
$('pasteUse').onclick = () => {
  const t = $('pasteBox').value.trim();
  if (!t) return;
  listenBuffer = t;
  listenSession = '';
  $('liveText').textContent = t;
  $('pasteBox').style.display = 'none';
  $('pasteUse').style.display = 'none';
  $('pasteBox').value = '';
  $('extractBtn').style.display = 'inline-block';
};

/* settings controls */
$('setUrl').value = S.url; $('setToken').value = S.token;
$('setSpeak').checked = S.speak; $('setAutoListen').checked = S.autoListen;
$('setWake').checked = S.wake; $('setRate').value = S.rate;
$('rateVal').textContent = (S.rate).toFixed(1) + '×';
$('setSpeak').onchange = (e) => { S.speak = e.target.checked; save(); };
$('setAutoListen').onchange = (e) => { S.autoListen = e.target.checked; save(); };
$('setWake').onchange = (e) => { S.wake = e.target.checked; save(); };
$('setRate').oninput = (e) => { S.rate = parseFloat(e.target.value);
  $('rateVal').textContent = S.rate.toFixed(1) + '×'; save(); };
$('setVoice').onchange = (e) => { S.voice = e.target.value; save(); };
$('saveConn').onclick = async () => {
  S.url = $('setUrl').value.trim(); S.token = $('setToken').value.trim(); save();
  $('connMsg').textContent = 'testing…';
  const ok = await ping();
  $('connMsg').textContent = ok ? '✓ connected' : '✗ could not connect';
  if (ok) { pollNotifs(); askNotifPermission(); }
};

function askNotifPermission() {
  if ('Notification' in window && Notification.permission === 'default')
    Notification.requestPermission().catch(() => {});
}

/* PWA install */
let deferredPrompt = null;
window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault();
  deferredPrompt = e; $('installBtn').style.display = 'inline-block'; });
$('installBtn').onclick = async () => { if (!deferredPrompt) return;
  deferredPrompt.prompt(); await deferredPrompt.userChoice; deferredPrompt = null;
  $('installBtn').style.display = 'none'; };

$('clearChat').onclick = () => { if (confirm('Clear the conversation on this phone?')) clearFeed(); };

/* v0.10: coming back from the background -> catch up immediately */
function onResume() {
  ping(); pollNotifs();
  if (UI.tab === 'followups') loadFollowups(true);
  if (UI.tab === 'rfq') loadRfqMeta();
}

/* voices load async */
if (window.speechSynthesis) {
  loadVoices();
  speechSynthesis.onvoiceschanged = loadVoices;
}

/* service worker */
if ('serviceWorker' in navigator)
  navigator.serviceWorker.register('sw.js').catch(() => {});

/* v0.10: restore everything first, then the usual banner only on a fresh start */
replayFeed();
restoreRfqDraft();
fuRestoreDraft();
(function restoreListen() {
  const d = LS.get('mbb_listen', null);
  if (d && d.buffer) { listenBuffer = d.buffer; $('liveText').textContent = d.buffer;
    $('extractBtn').style.display = 'inline-block'; }
})();
unseen = UI.unseen || 0; updateBadge();
if (UI.tab && UI.tab !== 'chat') showTab(UI.tab);
renderFollowups();

/* mic availability banner */
if (!FEED.length) {
  if (!speechSupported())
    addMsg('sys', 'Heads up: voice input needs Chrome with a secure connection. You can still type requests below. See Settings for how to enable the mic over Tailscale.');
  else
    addMsg('sys', 'Tap the mic and ask, e.g. “what\'s equal to a Topaz 100”, “what did we pay for 3/4 EMT”, then “show me the email”. Or “follow up with Thea tomorrow at 9”.');
}

/* boot */
(async function boot() {
  if (S.url || S.token) { await ping(); pollNotifs(); askNotifPermission();
    loadFollowups(true); }
  else setConn(false);
  setInterval(() => { if (!document.hidden) pollNotifs(); }, 20000);   // alerts
  setInterval(() => { if (!document.hidden && UI.tab === 'followups') loadFollowups(true); }, 45000);
})();
