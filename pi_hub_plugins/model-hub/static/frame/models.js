// Model Hub tab. Runs sandboxed: no network, storage, popups or confirm(), so
// everything goes through the `ph` bridge and dangerous clicks are two-step.
// Model and engine names come from the machine: only ever set as textContent.
(function () {
  'use strict';

  var POLL_MS = 3000, FAST_MS = 1000, ARM_MS = 4000, HISTORY = 120;
  var TYPE_ORDER = ['text', 'image', 'video', 'audio', 'embedding'];
  var STATE_LABEL = {
    running: 'Running', starting: 'Starting', stopping: 'Stopping',
    stopped: 'Stopped', failed: 'Failed', unavailable: 'Ollama is stopped'
  };
  // What the badge next to each name says: can I use this model right now?
  var PHASE_LABEL = {
    ready: 'Ready', loading: 'Loading…', unloading: 'Unloading…',
    off: 'Not loaded', failed: 'Failed', unavailable: 'Not loaded'
  };

  var data = null, filter = 'all', timer = 0, busy = false, visible = true;
  var armed = {};            // key -> expiry
  var pending = {};          // item id -> 'start' | 'stop' while a click is in flight
  var lastPhase = null;      // item id -> phase, to toast when a model turns ready
  var seenJobs = null;       // job id -> state, to toast each outcome once
  var listSig = '', jobSig = '', targetSig = '';
  var hist = { vram: [], busy: [] };

  function isAdmin() { return ph.user.role === 'admin'; }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  // ── Formatting ────────────────────────────────────────────────────────
  var GIB = 1073741824, MIB = 1048576;
  function bytes(n) {
    if (n == null) return '–';
    if (n < GIB) return Math.round(n / MIB) + ' MB';
    var g = n / GIB;
    return (g < 100 ? g.toFixed(1) : Math.round(g)) + ' GB';
  }
  function rate(n) { return n ? bytes(n) + '/s' : ''; }
  function pct(n) { return n == null ? '–' : Math.round(n) + ' %'; }
  function capitalize(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

  // ── Page skeleton (built once, so forms keep their input during polls) ─
  var root = el('div', 'hub');
  document.body.appendChild(root);

  var offline = el('div', 'offline');
  var main = el('div');
  root.appendChild(offline);
  root.appendChild(main);

  var notice = el('p', 'line');
  var alertLine = el('p', 'line bad alert');
  alertLine.setAttribute('role', 'alert');
  var tiles = el('div', 'tiles');
  var chips = el('div', 'chips');
  var list = el('div', 'list');
  main.appendChild(notice);
  main.appendChild(alertLine);
  main.appendChild(tiles);
  main.appendChild(chips);
  main.appendChild(list);

  // ── Monitor tiles ─────────────────────────────────────────────────────
  function makeSpark(fmt, histKey) {
    var NS = 'http://www.w3.org/2000/svg', W = 200, H = 34;
    var box = el('div', 'spark');
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.setAttribute('preserveAspectRatio', 'none');
    var line = document.createElementNS(NS, 'polyline');
    line.setAttribute('class', 'spark-line');
    var cross = document.createElementNS(NS, 'line');
    cross.setAttribute('class', 'spark-cross');
    cross.setAttribute('y1', 0); cross.setAttribute('y2', H);
    cross.style.display = 'none';
    svg.appendChild(line); svg.appendChild(cross);
    var tip = el('div', 'spark-tip');
    tip.style.display = 'none';
    box.appendChild(svg); box.appendChild(tip);
    var hover = null, max = 1;

    function x(i, n) { return n < 2 ? W : i * W / (HISTORY - 1) + (HISTORY - n) * W / (HISTORY - 1); }
    function paintHover() {
      var pts = hist[histKey];
      if (hover == null || !pts.length) { cross.style.display = 'none'; tip.style.display = 'none'; return; }
      var i = Math.max(0, Math.min(pts.length - 1, Math.round(hover * (HISTORY - 1)) - (HISTORY - pts.length)));
      var px = x(i, pts.length);
      cross.setAttribute('x1', px); cross.setAttribute('x2', px);
      cross.style.display = '';
      var ago = (pts.length - 1 - i) * POLL_MS / 1000;
      tip.textContent = fmt(pts[i]) + (ago ? ' · ' + Math.round(ago) + ' s ago' : ' · now');
      tip.style.display = '';
      tip.style.left = Math.min(px / W * 100, 62) + '%';
    }
    function point(e) {
      var r = svg.getBoundingClientRect();
      hover = Math.max(0, Math.min(1, (e.clientX - r.left) / (r.width || 1)));
      paintHover();
    }
    box.addEventListener('pointermove', point);
    box.addEventListener('pointerdown', point);
    box.addEventListener('pointerleave', function () { hover = null; paintHover(); });

    return {
      el: box,
      draw: function (scaleMax) {
        var pts = hist[histKey];
        max = Math.max(scaleMax || 0, 1);
        line.setAttribute('points', pts.map(function (v, i) {
          return x(i, pts.length).toFixed(1) + ',' + (H - 2 - (Math.min(v, max) / max) * (H - 4)).toFixed(1);
        }).join(' '));
        paintHover();
      }
    };
  }

  function makeTile(title) {
    var t = el('div', 'tile');
    t.appendChild(el('div', 'tile-title', title));
    t.value = el('div', 'tile-value', '–');
    t.sub = el('div', 'tile-sub');
    t.appendChild(t.value);
    t.appendChild(t.sub);
    tiles.appendChild(t);
    return t;
  }
  function meter(parent) {
    var track = el('div', 'meter'), fill = el('div', 'meter-fill');
    track.appendChild(fill);
    parent.appendChild(track);
    return function (ratio) {
      fill.style.width = Math.round(Math.max(0, Math.min(1, ratio)) * 100) + '%';
      fill.className = 'meter-fill' + (ratio >= 0.92 ? ' high' : '');
    };
  }

  var tVram = makeTile('Video memory'), tGpu = makeTile('GPU load');
  var tCpu = makeTile('CPU'), tRam = makeTile('Memory'), tDisk = makeTile('Disk');
  var vramMeter = meter(tVram), ramMeter = meter(tRam);
  var vramSpark = makeSpark(function (v) { return bytes(v); }, 'vram');
  var busySpark = makeSpark(function (v) { return pct(v); }, 'busy');
  tVram.appendChild(vramSpark.el);
  tGpu.appendChild(busySpark.el);
  var diskRows = el('div', 'disk-rows');
  tDisk.appendChild(diskRows);

  function drawTiles(d) {
    var g = d.gpu;
    if (g) {
      tVram.value.textContent = bytes(g.vram_used) + ' of ' + bytes(g.vram_total);
      vramMeter((g.vram_used || 0) / (g.vram_total || 1));
      tGpu.value.textContent = pct(g.busy);
      tGpu.sub.textContent = [g.temp != null ? Math.round(g.temp) + ' °C' : '',
                              g.power != null ? Math.round(g.power) + ' W' : ''].filter(Boolean).join(' · ');
      if (g.vram_used != null) push('vram', g.vram_used);
      if (g.busy != null) push('busy', g.busy);
      vramSpark.draw(g.vram_total);
      busySpark.draw(100);
    } else {
      tVram.value.textContent = 'Not available';
      tGpu.value.textContent = 'Not available';
    }
    tCpu.value.textContent = pct(d.cpu.percent);
    tCpu.sub.textContent = [d.cpu.temp != null ? Math.round(d.cpu.temp) + ' °C' : '',
                            d.cpu.cores ? d.cpu.cores + ' threads' : ''].filter(Boolean).join(' · ');
    tRam.value.textContent = bytes(d.ram.used) + ' of ' + bytes(d.ram.total);
    ramMeter(d.ram.used / (d.ram.total || 1));

    clear(diskRows);
    tDisk.value.textContent = d.disks.length ? bytes(d.disks[0].free) + ' free' : '–';
    tDisk.sub.textContent = d.disks.length ? d.disks[0].path : '';
    d.disks.slice(1).forEach(function (disk) {
      diskRows.appendChild(el('div', 'tile-sub', bytes(disk.free) + ' free on ' + disk.path));
    });
  }
  function push(key, v) {
    hist[key].push(v);
    if (hist[key].length > HISTORY) hist[key].shift();
  }

  // ── Actions ───────────────────────────────────────────────────────────
  // Inline as well as a toast: the bridge drops toasts sent within 5 s of another.
  var flashTimer = 0;
  function flash(message) {
    alertLine.textContent = message;
    clearTimeout(flashTimer);
    flashTimer = setTimeout(function () { alertLine.textContent = ''; }, 10000);
  }
  function fail(e) {
    var message = (e && e.message) || 'Request failed';
    flash(message);
    ph.toast(message, 'err');
  }
  function refreshSoon() { schedule(FAST_MS); }

  function act(id, path, body, kind) {
    pending[id] = kind;
    listSig = '';
    drawList();
    return ph.call(path, { method: 'POST', body: body }).catch(fail)
      .then(function () { delete pending[id]; listSig = ''; refreshSoon(); });
  }
  function start(item, force) { act(item.id, 'models/start', { id: item.id, force: !!force }, 'start'); }
  function stop(item) { act(item.id, 'models/stop', { id: item.id }, 'stop'); }

  function button(label, cls, onClick, disabled) {
    var b = el('button', 'btn ' + (cls || ''), label);
    b.type = 'button';
    b.disabled = !!disabled;
    b.addEventListener('click', onClick);
    return b;
  }
  // First click arms the button and says what will happen; the second acts.
  function armable(key, label, armedLabel, cls, onConfirm, disabled) {
    var on = armed[key] > Date.now();
    var b = button(on ? armedLabel : label, on ? 'armed' : cls, function () {
      if (armed[key] > Date.now()) {
        delete armed[key];
        listSig = '';
        onConfirm();
      } else {
        armed[key] = Date.now() + ARM_MS;
        listSig = '';
        drawList();
        setTimeout(function () { listSig = ''; drawList(); }, ARM_MS + 30);
      }
    }, disabled);
    return b;
  }

  // ── Model list ────────────────────────────────────────────────────────
  function jobFor(item) {
    return (data.jobs || []).filter(function (j) {
      return j.state === 'running' && (j.kind === 'start' || j.kind === 'stop') && j.label === item.name;
    })[0];
  }
  // ready | loading | unloading | off | failed | unavailable
  function phase(item) {
    var job = jobFor(item);
    var kind = job ? job.kind : pending[item.id];
    if (kind === 'start') return 'loading';
    if (kind === 'stop') return 'unloading';
    return { running: 'ready', starting: 'loading', stopping: 'unloading',
             failed: 'failed', unavailable: 'unavailable' }[item.state] || 'off';
  }
  // Ollama unloads idle models on its own; say when.
  function unloadsIn(item) {
    if (!item.until) return '';
    var t = Date.parse(String(item.until).replace(/(\.\d{3})\d+/, '$1'));
    var left = (t - Date.now()) / 60000;
    if (!(left > 0) || left > 24 * 60) return '';
    return left < 1 ? 'unloads in under a minute' : 'unloads in ' + Math.round(left) + ' min';
  }
  function isSwitching() {
    return (data.jobs || []).some(function (j) {
      return j.state === 'running' && (j.kind === 'start' || j.kind === 'stop');
    });
  }

  function drawChips(items) {
    var counts = {};
    items.forEach(function (i) { counts[i.type] = (counts[i.type] || 0) + 1; });
    var types = Object.keys(counts).sort(function (a, b) {
      var ai = TYPE_ORDER.indexOf(a), bi = TYPE_ORDER.indexOf(b);
      if (ai < 0) ai = 99; if (bi < 0) bi = 99;
      return ai - bi || (a < b ? -1 : 1);
    });
    if (filter !== 'all' && !counts[filter]) filter = 'all';
    clear(chips);
    [['all', 'All', items.length]].concat(types.map(function (t) {
      return [t, capitalize(t), counts[t]];
    })).forEach(function (c) {
      var b = button(c[1] + ' ' + c[2], 'chip' + (filter === c[0] ? ' on' : ''), function () {
        filter = c[0];
        listSig = '';
        drawList();
      });
      chips.appendChild(b);
    });
  }

  function itemRow(item, switching) {
    var p = phase(item);
    var row = el('div', 'row s-' + item.state + ' p-' + p);
    var head = el('div', 'row-main');
    var title = el('div', 'row-title');
    title.appendChild(el('span', 'dot'));
    title.appendChild(el('span', 'name', item.name));
    var badge = el('span', 'badge', PHASE_LABEL[p]);
    badge.setAttribute('role', 'status');
    title.appendChild(badge);
    var main = el('div', 'row-meta');
    var bits = [capitalize(item.type), item.runtime];
    if (item.size) bits.push(bytes(item.size));
    main.appendChild(el('span', '', bits.join(' · ')));
    head.appendChild(title);
    head.appendChild(main);

    var job = jobFor(item);
    var state = [];
    if (job) state.push(job.message || 'Working');
    else if (p !== 'ready' && p !== 'off') state.push(STATE_LABEL[item.state] || item.state);
    if (item.vram) state.push(bytes(item.vram) + ' on GPU');
    if (p === 'ready') state.push(unloadsIn(item));
    state = state.filter(Boolean);
    if (state.length) head.appendChild(el('div', 'row-state', state.join(' · ')));

    var extra = [];
    if (item.detail) extra.push(item.detail);
    if (item.tags && item.tags.length) extra.push(item.tags.join(', '));
    if (item.has_model_dirs && !(item.files || []).length) extra.push('No model files in its folders yet');
    if (extra.length) head.appendChild(el('div', 'row-detail', extra.join(' · ')));
    if (item.url && item.state === 'running') head.appendChild(el('div', 'row-url', item.url));
    row.appendChild(head);

    if (isAdmin()) {
      var actions = el('div', 'row-actions');
      var locked = !!pending[item.id] || switching;
      if (item.state === 'running' || item.state === 'starting') {
        actions.appendChild(button('Stop', 'stop', function () { stop(item); }, locked));
      } else if (item.state !== 'stopping') {
        var conflicts = item.conflicts || [];
        if (conflicts.length) {
          actions.appendChild(armable('start:' + item.id, 'Start',
            'Stops ' + conflicts.join(', ') + '. Click again', 'go',
            function () { start(item, true); }, locked));
        } else {
          actions.appendChild(button('Start', 'go', function () { start(item, false); }, locked));
        }
      }
      if (item.removable && item.state === 'stopped') {
        actions.appendChild(armable('rm:' + item.id, 'Remove', 'Remove? Click again', 'quiet', function () {
          ph.call('engines/' + item.id.slice('engine:'.length), { method: 'DELETE' })
            .catch(fail).then(refreshSoon);
        }, locked));
      }
      row.appendChild(actions);
    }
    return row;
  }

  function drawList() {
    if (!data || !data.items) return;
    var sig = JSON.stringify([filter, data.items, isAdmin(), pending, Math.floor(Date.now() / 60000),
      (data.jobs || []).filter(function (j) { return j.state === 'running' && j.kind !== 'pull' && j.kind !== 'file'; })
        .map(function (j) { return [j.label, j.message]; })]);
    if (sig === listSig) return;
    listSig = sig;

    drawChips(data.items);
    clear(list);
    var switching = isSwitching();
    var shown = data.items.filter(function (i) { return filter === 'all' || i.type === filter; });
    shown.sort(function (a, b) {
      var ar = a.state === 'running' ? 0 : 1, br = b.state === 'running' ? 0 : 1;
      return ar - br || (a.name < b.name ? -1 : 1);
    });
    shown.forEach(function (item) { list.appendChild(itemRow(item, switching)); });
    if (!shown.length) list.appendChild(el('p', 'line', 'No models found yet.'));
  }

  // ── Downloads ─────────────────────────────────────────────────────────
  var dl = el('section', 'panel');
  dl.appendChild(el('h2', '', 'Download'));
  var form = el('div', 'form');
  var kind = el('select');
  [['ollama', 'Ollama model'], ['file', 'File from URL']].forEach(function (o) {
    var opt = el('option', '', o[1]); opt.value = o[0]; kind.appendChild(opt);
  });
  var source = el('input');
  source.spellcheck = false;
  source.setAttribute('aria-label', 'Model name or URL');
  var target = el('select');
  target.setAttribute('aria-label', 'Download folder');
  var filename = el('input');
  filename.placeholder = 'File name (optional)';
  filename.spellcheck = false;
  var dlBtn = button('Download', 'primary', submitDownload);
  [kind, source, target, filename, dlBtn].forEach(function (n) { form.appendChild(n); });
  dl.appendChild(form);
  var jobsBox = el('div', 'jobs');
  dl.appendChild(jobsBox);
  main.appendChild(dl);

  function syncKind() {
    var isFile = kind.value === 'file';
    source.placeholder = isFile ? 'https://huggingface.co/org/repo/resolve/main/model.gguf'
                                : 'qwen2.5-coder:7b or hf.co/org/repo:Q4_K_M';
    target.style.display = isFile ? '' : 'none';
    filename.style.display = isFile ? '' : 'none';
  }
  kind.addEventListener('change', syncKind);
  syncKind();

  function submitDownload() {
    var value = source.value.trim();
    if (!value) { ph.toast('Enter a model name or URL', 'err'); return; }
    var body = kind.value === 'ollama' ? { kind: 'ollama', name: value }
      : { kind: 'file', url: value, target: target.value, filename: filename.value.trim() };
    dlBtn.disabled = true;
    ph.call('download', { method: 'POST', body: body })
      .then(function () { source.value = ''; filename.value = ''; })
      .catch(fail)
      .then(function () { dlBtn.disabled = false; refreshSoon(); });
  }

  function drawTargets(targets) {
    var sig = JSON.stringify(targets);
    if (sig === targetSig) return;
    targetSig = sig;
    var keep = target.value;
    clear(target);
    targets.forEach(function (t) {
      var opt = el('option', '', t.engine + ' · ' + t.label);
      opt.value = t.id;
      target.appendChild(opt);
    });
    if (keep) target.value = keep;
  }

  function drawJobs(jobs) {
    var downloads = jobs.filter(function (j) { return j.kind === 'pull' || j.kind === 'file'; }).slice(0, 8);
    var sig = JSON.stringify(downloads);
    if (sig === jobSig) return;
    jobSig = sig;
    clear(jobsBox);
    downloads.forEach(function (j) {
      var row = el('div', 'job j-' + j.state);
      var top = el('div', 'job-top');
      top.appendChild(el('span', 'name', j.label));
      var parts = [];
      if (j.total) parts.push(bytes(j.done) + ' of ' + bytes(j.total));
      if (j.state === 'running' && j.speed) parts.push(rate(j.speed));
      if (j.state !== 'running' || !j.total) parts.push(j.message || capitalize(j.state));
      top.appendChild(el('span', 'job-info', parts.join(' · ')));
      if (j.state === 'running' && isAdmin()) {
        top.appendChild(button('Cancel', 'quiet', function () {
          ph.call('jobs/' + j.id + '/cancel', { method: 'POST' }).catch(fail).then(refreshSoon);
        }));
      }
      row.appendChild(top);
      if (j.state === 'running' && j.total) meter(row)(j.done / j.total);
      jobsBox.appendChild(row);
    });
  }

  // ── Add an engine ─────────────────────────────────────────────────────
  var add = el('details', 'panel');
  add.appendChild(el('summary', '', 'Add an engine'));
  add.appendChild(el('p', 'line', 'Anything that runs as a systemd service on the PC. Ollama models and files in known folders appear on their own.'));
  function field(placeholder, label) {
    var i = el('input');
    i.placeholder = placeholder;
    i.spellcheck = false;
    i.setAttribute('aria-label', label || placeholder);
    return i;
  }
  var addForm = el('div', 'form');
  var fName = field('Name'), fType = field('Type: image, audio, 3d, …', 'Type');
  var fUnit = field('Service, for example whisper.service', 'Systemd service');
  var fPort = field('Port (optional)'), fUrl = field('Web address (optional)');
  var fGpu = el('label', 'check');
  var fExclusive = el('input');
  fExclusive.type = 'checkbox';
  fExclusive.checked = true;
  fGpu.appendChild(fExclusive);
  fGpu.appendChild(document.createTextNode(' Needs the whole GPU'));
  var addBtn = button('Add', 'primary', submitAdd);
  [fName, fType, fUnit, fPort, fUrl, fGpu, addBtn].forEach(function (n) { addForm.appendChild(n); });
  add.appendChild(addForm);
  add.appendChild(el('p', 'line', 'For a plain command without a service, put one JSON file per engine into /etc/model-agent/engines.d on the PC. It shows up here within seconds.'));
  main.appendChild(add);

  function submitAdd() {
    addBtn.disabled = true;
    ph.call('engines', { method: 'POST', body: {
      name: fName.value.trim(), type: fType.value.trim().toLowerCase(), unit: fUnit.value.trim(),
      port: fPort.value.trim(), url: fUrl.value.trim(), exclusive: fExclusive.checked
    } }).then(function () {
      [fName, fType, fUnit, fPort, fUrl].forEach(function (i) { i.value = ''; });
      add.open = false;
      ph.toast('Engine added', 'ok');
    }).catch(fail).then(function () { addBtn.disabled = false; refreshSoon(); });
  }

  // ── Offline view ──────────────────────────────────────────────────────
  function drawOffline(d) {
    main.style.display = 'none';
    offline.style.display = '';
    clear(offline);
    offline.appendChild(el('h2', '', 'The model PC is not reachable'));
    offline.appendChild(el('p', 'line', (d.error || 'No answer') + '. The PC may be switched off.'));
    if (d.can_wake && isAdmin()) {
      var b = button('Wake PC', 'go', function () {
        b.disabled = true;
        ph.call('wake', { method: 'POST' })
          .then(function () { ph.toast('Wake signal sent', 'ok'); })
          .catch(fail).then(function () { b.disabled = false; });
      });
      offline.appendChild(b);
    }
  }

  // ── Job outcomes ──────────────────────────────────────────────────────
  function announce(jobs) {
    var first = seenJobs === null;
    var next = {};
    jobs.forEach(function (j) {
      next[j.id] = j.state;
      if (first || j.state === 'running') return;
      if (seenJobs[j.id] === 'running' || (seenJobs[j.id] === undefined)) {
        if (j.state === 'error') fail(new Error(j.label + ': ' + (j.message || 'failed')));
        else if (j.state === 'done' && (j.kind === 'pull' || j.kind === 'file')) ph.toast(j.label + ' downloaded', 'ok');
      }
    });
    seenJobs = next;
  }

  // Toast the moment a model turns ready, or drops out of memory.
  function announceReady(items) {
    var next = {};
    items.forEach(function (item) {
      var p = phase(item);
      next[item.id] = p;
      var before = lastPhase && lastPhase[item.id];
      if (!before || before === p) return;
      if (p === 'ready') ph.toast(item.name + ' is ready', 'ok');
      else if (before === 'ready' && p === 'off') ph.toast(item.name + ' is no longer loaded', 'ok');
    });
    lastPhase = next;
  }

  // ── Poll ──────────────────────────────────────────────────────────────
  function draw(d) {
    data = d;
    if (!d.reachable) { drawOffline(d); return; }
    offline.style.display = 'none';
    main.style.display = '';

    var messages = [];
    if (d.active_engine) messages.push(d.active_engine + ' holds the GPU');
    else if (!d.ollama.up) messages.push('Ollama is stopped');
    notice.textContent = messages.join(' · ');
    notice.className = 'line' + (!d.ollama.up && !d.active_engine ? ' warn' : '');
    (d.catalog_errors || []).forEach(function (err) {
      var p = el('p', 'line bad', 'Engine file skipped: ' + err);
      notice.appendChild(document.createElement('br'));
      notice.appendChild(p);
    });

    dl.style.display = add.style.display = isAdmin() ? '' : 'none';
    drawTiles(d);
    drawList();
    drawTargets(d.targets || []);
    drawJobs(d.jobs || []);
    announce(d.jobs || []);
    announceReady(d.items || []);
  }

  function poll() {
    if (busy || !visible) return;
    busy = true;
    ph.call('status').then(draw).catch(function (e) {
      drawOffline({ error: (e && e.message) || 'Request failed' });
    }).then(function () {
      busy = false;
      var jobsNow = (data && data.jobs) || [];
      // Poll every second while something loads, so "Ready" shows up right away.
      var switching = data && data.items && (isSwitching() || data.items.some(function (i) {
        return i.state === 'starting' || i.state === 'stopping';
      }));
      var hot = jobsNow.some(function (j) { return j.state === 'running'; });
      schedule(switching ? FAST_MS : hot ? FAST_MS * 2 : POLL_MS);
    });
  }
  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(poll, ms);
  }

  ph.on('visible', function (v) { visible = v; if (v) schedule(0); });
  ph.on('refresh', function () { schedule(0); });
  ph.on('theme', function () { listSig = ''; drawList(); });
  schedule(0);
})();
