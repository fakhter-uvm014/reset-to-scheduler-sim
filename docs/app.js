/* reset-to-scheduler-sim viewer (vanilla JS, no build step).
 * Loads traces/index.json, then one trace per scenario, and lets you step
 * through the events with the registers, stacks, fault decoder and cycle
 * counters updating at each step.
 * Author: Farzana Akhter
 */
"use strict";

(function () {
  // ------------------------------------------------------------------ helpers
  const $ = (id) => document.getElementById(id);
  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") n.className = v;
        else if (k === "text") n.textContent = v;
        else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
        else n.setAttribute(k, v === true ? "" : v);
      }
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      n.appendChild(typeof kid === "string" || typeof kid === "number" ? document.createTextNode(String(kid)) : kid);
    }
    return n;
  }
  const svgNS = "http://www.w3.org/2000/svg";
  function sv(tag, attrs) {
    const n = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    return n;
  }
  const hex = (s) => (typeof s === "string" ? parseInt(s, 16) : s) >>> 0;
  const h8 = (n) => "0x" + (n >>> 0).toString(16).toUpperCase().padStart(8, "0");
  const fmt = (n) => (typeof n === "number" ? n.toLocaleString("en-US") : String(n));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } },
  };

  const EXC = { 1: "Reset", 2: "NMI", 3: "HardFault", 4: "MemManage", 5: "BusFault", 6: "UsageFault",
    7: "SecureFault", 11: "SVCall", 12: "DebugMonitor", 14: "PendSV", 15: "SysTick", 16: "IRQ_TIMER0", 37: "IRQ_GPIO" };
  const excName = (n) => EXC[n] || (n >= 16 ? "IRQ" + (n - 16) : "Exc" + n);
  const FAULTS = ["HardFault", "MemManage", "BusFault", "UsageFault"];
  const EXC_RETURN = {
    "0xFFFFFFF1": "return to handler mode, frame on MSP (this handler pre-empted another handler)",
    "0xFFFFFFF9": "return to thread mode, frame on MSP (interrupted main or privileged MSP code)",
    "0xFFFFFFFD": "return to thread mode, frame on PSP (interrupted a task)",
  };
  const KIND_HELP = {
    exception_entry: "The core stacked r0-r3, r12, lr, pc, xPSR, put EXC_RETURN in LR and jumped to the vector.",
    tail_chain: "Back-to-back handlers: the frame underneath stays on the stack, saving the pop and push.",
    exception_return: "bx lr with EXC_RETURN: the core popped the frame (or tail-chained).",
    context_switch: "PendSV saved the outgoing task's r4-r11, the scheduler picked the next task, its registers were loaded.",
    fault: "A fault was detected: the status registers below say exactly why.",
    lockup: "A fault where nothing can handle it: the core stops until reset.",
    syscall: "svc: the only door from an unprivileged task into the kernel.",
    boot_step: "One of the six boot milestones, checked against the image.",
    critical_section: "PRIMASK was set while an interrupt waited: that wait is part of its latency.",
    privilege: "CONTROL changed, or an unprivileged attempt at a privileged operation was ignored.",
    sleep: "wfi: the core slept until the next interrupt.",
  };

  // -------------------------------------------------------------------- state
  const S = {
    index: null, trace: null, variant: null, events: [], regs: [], sel: 0, cursorA: null,
    scale: 1, fitScale: 1, mystery: store.get("r2s-mystery") === "1", revealed: false,
    stackTask: null, segments: [], lanes: [], endCycles: 1,
  };

  // -------------------------------------------------------------------- theme
  function applyThemeLabel() {
    const t = document.documentElement.getAttribute("data-theme") || "auto";
    $("theme").textContent = "Theme: " + t;
  }
  $("theme").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme") || "auto";
    const next = cur === "auto" ? "light" : cur === "light" ? "dark" : "auto";
    if (next === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", next);
    store.set("r2s-theme", next);
    applyThemeLabel();
  });
  applyThemeLabel();

  // ------------------------------------------------------------------ loading
  function showError(html) {
    const box = $("loadError");
    box.innerHTML = html;
    box.classList.remove("hidden");
  }

  async function init() {
    $("mystery").checked = S.mystery;
    try {
      const res = await fetch("traces/index.json", { cache: "no-cache" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      S.index = await res.json();
    } catch (err) {
      showError("<strong>Could not load <code>traces/index.json</code>.</strong> Browsers block <code>fetch()</code> " +
        "for pages opened as files. Serve the folder instead: <code>cd docs</code> then " +
        "<code>python -m http.server 8000</code> and open <code>http://localhost:8000/</code>. " +
        "If the file is missing, run <code>python -m r2s run-all</code> first.");
      return;
    }
    buildPicker();
    const [id, variant] = decodeURIComponent(location.hash.slice(1)).split(":");
    const known = S.index.scenarios.find((s) => s.id === id);
    await loadScenario(known ? known.id : S.index.scenarios[0].id, variant || null);
  }

  function buildPicker() {
    const sel = $("scenario");
    sel.textContent = "";
    const groups = {};
    for (const s of S.index.scenarios) (groups[s.layer] = groups[s.layer] || []).push(s);
    for (const layer of Object.keys(groups).sort()) {
      const og = el("optgroup", { label: layerLabel(layer) });
      for (const s of groups[layer]) {
        const label = S.mystery && !(S.trace && S.trace.scenario.id === s.id && S.revealed)
          ? s.id + " (mystery)" : s.id + "  " + s.title;
        og.appendChild(el("option", { value: s.id, text: label }));
      }
      sel.appendChild(og);
    }
    if (S.trace) sel.value = S.trace.scenario.id;
  }

  async function loadScenario(id, variant) {
    const entry = S.index.scenarios.find((s) => s.id === id);
    try {
      const res = await fetch("traces/" + entry.file, { cache: "no-cache" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      S.trace = await res.json();
    } catch (err) {
      showError("Could not load <code>traces/" + entry.file + "</code> (" + err.message + "). Run <code>python -m r2s run-all</code>.");
      return;
    }
    $("loadError").classList.add("hidden");
    S.revealed = false;
    S.cursorA = null;
    $("scenario").value = id;
    const variants = S.trace.variants || [];
    const vsel = $("variant");
    vsel.textContent = "";
    for (const v of variants) vsel.appendChild(el("option", { value: v, text: v }));
    $("variantWrap").classList.toggle("hidden", variants.length === 0);
    const v = variants.length ? (variants.includes(variant) ? variant : variants[0]) : null;
    if (v) vsel.value = v;
    selectVariant(v);
  }

  function summaryFor(v) {
    const s = S.trace.summary;
    return s.variants ? s.variants[v] : s;
  }

  function selectVariant(v) {
    S.variant = v;
    const all = S.trace.events;
    S.events = all.filter((e) => (e.details && e.details.variant !== undefined ? e.details.variant : null) === v);
    // full register file at every event: replay the per-event deltas
    S.regs = [];
    let cur = {};
    for (const e of S.events) {
      cur = Object.assign({}, cur, e.regs || {});
      S.regs.push(cur);
    }
    S.stackTask = null;
    const id = S.trace.scenario.id;
    history.replaceState(null, "", "#" + id + (v ? ":" + v : ""));
    renderHeader();
    buildTimeline();
    buildKindFilter();
    renderLog();
    renderMeasurements();
    renderResult();
    renderNotes();
    const firstFault = S.events.findIndex((e) => e.kind === "fault");
    select(firstFault >= 0 && !isMystery() ? firstFault : 0, true);
  }

  const isMystery = () => S.mystery && !S.revealed;
  const layerLabel = (layer) => (/^\d/.test(layer) ? "Layer " + layer : layer);

  // ------------------------------------------------------------------- header
  function renderHeader() {
    const sc = S.trace.scenario;
    const variant = (sc.variants || []).find((x) => x.name === S.variant);
    const symptom = (variant && variant.hidden_symptom) || sc.hidden_symptom;
    const pass = S.trace.result.passed;
    for (const p of [$("pill"), $("pill2")]) {
      p.textContent = pass ? "PASS" : "FAIL";
      p.className = "pill " + (pass ? "pass" : "fail");
    }
    if (isMystery()) {
      $("title").textContent = sc.id + ": " + symptom;
      $("desc").textContent = "Mystery mode: only the symptom is shown. Use the timeline, registers, stacks and the fault decoder to find the cause, then press Reveal in the learning notes.";
      $("injected").textContent = "";
    } else {
      $("title").textContent = sc.id + ": " + sc.title + "  [" + layerLabel(sc.layer) + "]";
      $("desc").textContent = sc.description;
      const inj = $("injected");
      inj.textContent = "";
      const faults = (sc.faults || []).concat(variant && variant.faults ? variant.faults : []);
      for (const f of faults) {
        const extra = Object.entries(f).filter(([k]) => k !== "type").map(([k, v]) => k + "=" + v).join(", ");
        inj.appendChild(el("li", null, "Injected fault: ", el("code", { text: f.type }), extra ? " (" + extra + ")" : ""));
      }
    }
    $("expected").textContent = isMystery() ? "(hidden in mystery mode)" : sc.expected.summary;
    buildPicker();
  }

  // ----------------------------------------------------------------- timeline
  function laneOf(ctx) {
    if (!ctx) return { key: "?", label: "?", cls: "thread", order: 99 };
    if (ctx === "boot") return { key: ctx, label: "Reset_Handler", cls: "thread", order: 0 };
    if (ctx === "main") return { key: ctx, label: "main", cls: "thread", order: 1 };
    const m = /^(ISR|kernel):(.*)$/.exec(ctx);
    if (m) {
      const name = m[2];
      const cls = FAULTS.includes(name) ? "fault" : (name === "PendSV" || name === "SVCall") ? "kernel" : "irq";
      const rank = { SVCall: 0, SysTick: 1, PendSV: 2 }[name];
      const order = cls === "fault" ? 40 + FAULTS.indexOf(name) : rank !== undefined ? 20 + rank : 30;
      return { key: "H:" + name, label: "Handler: " + name, cls, order };
    }
    return { key: ctx, label: "Task " + ctx, cls: "thread", order: ctx === "idle" ? 9 : 2 };
  }

  function buildTimeline() {
    const ev = S.events;
    const summary = summaryFor(S.variant) || {};
    S.endCycles = Math.max(1, summary.cycles || 0, ev.length ? ev[ev.length - 1].t_cycles : 1);
    const lanes = new Map();
    const segs = [];
    for (let i = 0; i < ev.length; i++) {
      const e = ev[i];
      const lane = laneOf(e.ctx);
      if (!lanes.has(lane.key)) lanes.set(lane.key, lane);
      const t0 = e.t_cycles;
      const t1 = i + 1 < ev.length ? ev[i + 1].t_cycles : S.endCycles;
      const sleep = e.kind === "sleep";
      const last = segs[segs.length - 1];
      if (last && last.lane === lane.key && last.sleep === sleep && last.t1 === t0) {
        last.t1 = t1;
      } else if (t1 >= t0) {
        segs.push({ lane: lane.key, cls: sleep ? "sleep" : lane.cls, t0, t1, i, sleep, ctx: e.ctx });
      }
    }
    S.lanes = Array.from(lanes.values()).sort((a, b) => a.order - b.order || a.label.localeCompare(b.label));
    S.segments = segs;
    const labels = $("tlLabels");
    labels.textContent = "";
    labels.appendChild(el("div", { class: "axis", text: "cycles" }));
    labels.appendChild(el("div", { text: "events" }));
    for (const l of S.lanes) labels.appendChild(el("div", { text: l.label, title: l.label }));
    fitTimeline();
  }

  function fitTimeline() {
    const w = Math.max(200, $("tlScroll").clientWidth - 4);
    S.fitScale = w / S.endCycles;
    S.scale = S.fitScale;
    drawTimeline();
  }

  function drawTimeline() {
    const svg = $("timeline");
    svg.textContent = "";
    const laneH = 22, axisH = 18, tickH = 22;
    const width = Math.max(200, Math.ceil(S.endCycles * S.scale) + 4);
    const height = axisH + tickH + S.lanes.length * laneH;
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);
    svg.setAttribute("viewBox", "0 0 " + width + " " + height);
    const x = (t) => 2 + t * S.scale;
    const laneY = {};
    S.lanes.forEach((l, k) => {
      laneY[l.key] = axisH + tickH + k * laneH;
      svg.appendChild(sv("rect", { class: "lane-bg" + (k % 2 ? " alt" : ""), x: 0, y: laneY[l.key], width, height: laneH }));
    });
    // axis
    const axis = sv("g", { class: "axis" });
    const step = niceStep(S.endCycles / Math.max(2, width / 110));
    for (let t = 0; t <= S.endCycles; t += step) {
      axis.appendChild(sv("line", { x1: x(t), x2: x(t), y1: axisH - 4, y2: height }));
      const tx = sv("text", { x: x(t) + 2, y: 11 });
      tx.textContent = t >= 1e6 ? (t / 1e6) + "M" : t >= 1e3 ? (t / 1e3) + "k" : String(t);
      axis.appendChild(tx);
    }
    svg.appendChild(axis);
    // event ticks
    S.events.forEach((e, i) => {
      if (!["fault", "lockup", "context_switch", "marker", "boot_step"].includes(e.kind)) return;
      const t = sv("line", { class: "tick " + e.kind, x1: x(e.t_cycles), x2: x(e.t_cycles), y1: axisH + 3, y2: axisH + tickH - 3 });
      t.dataset.i = i;
      svg.appendChild(t);
    });
    // segments
    const frag = document.createDocumentFragment();
    S.segments.forEach((s, k) => {
      const w = Math.max(1, (s.t1 - s.t0) * S.scale);
      const r = sv("rect", { class: "seg " + s.cls, x: x(s.t0), y: laneY[s.lane] + 4, width: w, height: laneH - 8, rx: 2 });
      r.dataset.k = k;
      frag.appendChild(r);
    });
    svg.appendChild(frag);
    // cursors
    if (S.cursorA !== null && S.events[S.cursorA]) {
      const tA = x(S.events[S.cursorA].t_cycles);
      svg.appendChild(sv("line", { class: "cursorA", x1: tA, x2: tA, y1: 0, y2: height }));
    }
    const cur = S.events[S.sel];
    if (cur) {
      const tc = x(cur.t_cycles);
      svg.appendChild(sv("line", { id: "cursor", class: "cursor", x1: tc, x2: tc, y1: 0, y2: height }));
    }
  }

  function niceStep(raw) {
    const p = Math.pow(10, Math.floor(Math.log10(Math.max(1, raw))));
    for (const m of [1, 2, 5, 10]) if (m * p >= raw) return m * p;
    return 10 * p;
  }

  function moveCursor() {
    const svg = $("timeline");
    const c = svg.querySelector("#cursor");
    const e = S.events[S.sel];
    if (!c || !e) { drawTimeline(); return; }
    const xx = 2 + e.t_cycles * S.scale;
    c.setAttribute("x1", xx);
    c.setAttribute("x2", xx);
    const box = $("tlScroll");
    if (xx < box.scrollLeft + 20 || xx > box.scrollLeft + box.clientWidth - 20) box.scrollLeft = Math.max(0, xx - box.clientWidth / 2);
  }

  function zoom(f) {
    const box = $("tlScroll");
    const e = S.events[S.sel];
    const center = e ? e.t_cycles : (box.scrollLeft + box.clientWidth / 2) / S.scale;
    S.scale = Math.min(Math.max(S.fitScale, S.scale * f), 8);
    drawTimeline();
    box.scrollLeft = Math.max(0, center * S.scale - box.clientWidth / 2);
  }
  $("zoomIn").addEventListener("click", () => zoom(2));
  $("zoomOut").addEventListener("click", () => zoom(0.5));
  $("zoomFit").addEventListener("click", fitTimeline);

  const tip = $("tooltip");
  $("timeline").addEventListener("mousemove", (ev) => {
    const t = ev.target;
    let html = null;
    if (t.dataset && t.dataset.k !== undefined) {
      const s = S.segments[+t.dataset.k];
      const lane = laneOf(s.ctx);
      const dur = s.t1 - s.t0;
      html = lane.label + (s.sleep ? " (sleeping)" : "") + "\ncycles " + fmt(s.t0) + " .. " + fmt(s.t1) +
        "\n" + fmt(dur) + " cycles = " + (dur / mhz()).toFixed(2) + " us";
    } else if (t.dataset && t.dataset.i !== undefined) {
      const e = S.events[+t.dataset.i];
      html = "#" + e.seq + " " + e.kind + " @" + fmt(e.t_cycles) + "\n" + e.reason.slice(0, 140);
    }
    if (!html) { tip.classList.add("hidden"); return; }
    tip.textContent = html;
    tip.style.whiteSpace = "pre-wrap";
    tip.classList.remove("hidden");
    const pad = 12;
    let left = ev.clientX + pad, top = ev.clientY + pad;
    const r = tip.getBoundingClientRect();
    if (left + r.width > window.innerWidth - 4) left = ev.clientX - r.width - pad;
    if (top + r.height > window.innerHeight - 4) top = ev.clientY - r.height - pad;
    tip.style.left = Math.max(4, left) + "px";
    tip.style.top = Math.max(4, top) + "px";
  });
  $("timeline").addEventListener("mouseleave", () => tip.classList.add("hidden"));
  $("timeline").addEventListener("click", (ev) => {
    const t = ev.target;
    if (t.dataset && t.dataset.k !== undefined) select(S.segments[+t.dataset.k].i);
    else if (t.dataset && t.dataset.i !== undefined) select(+t.dataset.i);
    else {
      const rect = $("timeline").getBoundingClientRect();
      const cyc = (ev.clientX - rect.left - 2) / S.scale;
      let best = 0;
      for (let i = 0; i < S.events.length; i++) if (S.events[i].t_cycles <= cyc) best = i;
      select(best);
    }
  });

  function mhz() {
    const hz = (S.trace && S.trace.config && S.trace.config.cpu_hz) || 150000000;
    const s = summaryFor(S.variant);
    if (s && s.tick && s.tick.actual_hz) return s.tick.actual_hz / 1e6;
    return hz / 1e6;
  }

  // ------------------------------------------------------------------ stepping
  function matchesStep(e, kind) {
    if (!kind) return true;
    if (kind === "exception") return ["exception_entry", "tail_chain", "exception_return"].includes(e.kind);
    return e.kind === kind;
  }
  function step(dir) {
    const kind = $("stepKind").value;
    for (let i = S.sel + dir; i >= 0 && i < S.events.length; i += dir) {
      if (matchesStep(S.events[i], kind)) { select(i); return; }
    }
  }
  $("prev").addEventListener("click", () => step(-1));
  $("next").addEventListener("click", () => step(1));
  $("first").addEventListener("click", () => select(0));
  $("last").addEventListener("click", () => select(S.events.length - 1));
  $("setA").addEventListener("click", () => {
    S.cursorA = S.cursorA === S.sel ? null : S.sel;
    drawTimeline();
    renderCounters();
  });
  document.addEventListener("keydown", (ev) => {
    if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
    if (ev.key === "ArrowRight") { step(1); ev.preventDefault(); }
    if (ev.key === "ArrowLeft") { step(-1); ev.preventDefault(); }
  });

  function select(i, initial) {
    if (!S.events.length) return;
    S.sel = Math.max(0, Math.min(S.events.length - 1, i));
    if (initial) drawTimeline(); else moveCursor();
    renderEventLine();
    renderCounters();
    renderRegisters();
    renderStack();
    renderDecoder();
    highlightLogRow(!initial);
  }

  function reasonText(e) {
    if (isMystery() && e.kind === "scenario") return "(scenario details hidden in mystery mode)";
    return e.reason;
  }

  function renderEventLine() {
    const e = S.events[S.sel];
    const box = $("evline");
    box.textContent = "";
    box.appendChild(el("span", { class: "mono" }, "#" + e.seq + "  ", el("span", { class: "tag-k " + e.kind, text: e.kind }),
      "  " + (e.from || "-") + " → " + (e.to || "-") + "   (" + (S.sel + 1) + " of " + S.events.length + ")"));
    box.appendChild(el("span", { class: "reason", text: reasonText(e) }));
    if (KIND_HELP[e.kind]) box.appendChild(el("span", { class: "reason muted small", text: KIND_HELP[e.kind] }));
  }

  function counter(value, label) {
    return el("div", { class: "counter" }, el("b", { text: value }), el("span", { text: label }));
  }

  function renderCounters() {
    const e = S.events[S.sel];
    const prev = S.sel > 0 ? S.events[S.sel - 1] : null;
    const box = $("counters");
    box.textContent = "";
    box.appendChild(counter(fmt(e.t_cycles), "CYCCNT (core cycles)"));
    box.appendChild(counter((e.t_cycles / mhz()).toFixed(3) + " µs", "time at " + mhz() + " MHz"));
    box.appendChild(counter(prev ? "+" + fmt(e.t_cycles - prev.t_cycles) : "-", "cycles since previous event"));
    if (S.cursorA !== null && S.events[S.cursorA]) {
      const d = e.t_cycles - S.events[S.cursorA].t_cycles;
      box.appendChild(counter((d >= 0 ? "+" : "") + fmt(d) + " cyc", "from cursor A (#" + S.events[S.cursorA].seq + ") = " + (d / mhz()).toFixed(2) + " µs"));
    } else {
      box.appendChild(counter("-", "cursor A: select an event, press Set cursor A, then step"));
    }
    $("setA").textContent = S.cursorA === S.sel ? "Clear cursor A" : "Set cursor A";
  }

  // ---------------------------------------------------------------- registers
  function renderRegisters() {
    const r = S.regs[S.sel] || {};
    const changed = S.events[S.sel].regs || {};
    const xpsr = hex(r.xpsr || 0);
    const ipsr = xpsr & 0x1ff;
    const control = +r.control || 0;
    const handler = ipsr !== 0;
    const usePsp = !handler && (control & 2);
    const priv = handler || !(control & 1);
    const box = $("regs");
    box.textContent = "";
    const names = ["r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11", "r12"];
    const cell = (label, value, isChanged, title) =>
      el("div", { class: isChanged ? "changed" : null, title: title || null }, el("span", { text: label }), el("span", { text: value }));
    for (const n of names) box.appendChild(cell(n, r[n] || "?", n in changed));
    box.appendChild(cell("sp (" + (usePsp ? "psp" : "msp") + ")", usePsp ? r.psp : r.msp, ("msp" in changed && !usePsp) || ("psp" in changed && usePsp)));
    box.appendChild(cell("msp", r.msp || "?", "msp" in changed, "main stack pointer: handlers, main"));
    box.appendChild(cell("psp", r.psp || "?", "psp" in changed, "process stack pointer: tasks"));
    box.appendChild(cell("lr", r.lr || "?", "lr" in changed));
    box.appendChild(cell("pc", r.pc || "?", "pc" in changed));
    box.appendChild(cell("xPSR", r.xpsr || "?", "xpsr" in changed));
    box.appendChild(cell("CONTROL", String(control), "control" in changed));
    box.appendChild(cell("PRIMASK", String(r.primask || 0), "primask" in changed));
    $("regMode").textContent = (handler ? "handler" : "thread") + " mode, " + (priv ? "privileged" : "unprivileged");
    const dec = $("regDecode");
    dec.textContent = "";
    const flag = (b) => (xpsr >>> b) & 1;
    dec.appendChild(el("div", null, el("b", { text: "xPSR " }), "N=" + flag(31) + " Z=" + flag(30) + " C=" + flag(29) + " V=" + flag(28) +
      "  T=" + flag(24) + (flag(24) ? "" : " (Thumb bit clear!)") + "  IPSR=" + ipsr + (ipsr ? " (" + excName(ipsr) + ")" : " (thread)")));
    dec.appendChild(el("div", null, el("b", { text: "CONTROL " }), "nPRIV=" + (control & 1) + " (" + (control & 1 ? "unprivileged" : "privileged") +
      " thread), SPSEL=" + ((control >> 1) & 1) + " (thread uses " + (control & 2 ? "PSP" : "MSP") + ")"));
    const lr = (r.lr || "").toUpperCase().replace("0X", "0x");
    if (EXC_RETURN[lr]) dec.appendChild(el("div", null, el("b", { text: "LR = EXC_RETURN " }), EXC_RETURN[lr]));
    else if (r.lr && hex(r.lr) & 1) dec.appendChild(el("div", null, el("b", { text: "LR " }), "return address (bit 0 = Thumb)"));
    dec.appendChild(el("div", { class: "muted" }, "Highlighted: changed at this event. Context: " + (S.events[S.sel].ctx || "-")));
  }

  // -------------------------------------------------------------------- stack
  function layout() {
    const s = summaryFor(S.variant) || {};
    return s.kernel && s.kernel.layout;
  }

  function renderStack() {
    const box = $("stack");
    box.textContent = "";
    const lay = layout();
    let snapIdx = -1;
    for (let i = S.sel; i >= 0; i--) if (S.events[i].details && S.events[i].details.stacks) { snapIdx = i; break; }
    if (lay && snapIdx >= 0) return renderTaskStacks(box, lay, S.events[snapIdx], snapIdx);
    let frameIdx = -1;
    for (let i = S.sel; i >= 0; i--) {
      const d = S.events[i].details;
      if (S.events[i].kind === "exception_entry" && d && d.frame_at) { frameIdx = i; break; }
    }
    if (frameIdx >= 0) renderFrame(box, S.events[frameIdx]);
    else box.appendChild(el("p", { class: "muted", text: "No exception frame yet at this point. Step forward to the first exception entry, or look at the memory map below." }));
    renderImageMap(box);
  }

  function renderFrame(box, e) {
    const d = e.details;
    const base = hex(d.frame_at);
    box.appendChild(el("p", { class: "small" }, "Exception frame pushed by the core for ", el("b", { text: d.name }),
      " at cycle " + fmt(e.t_cycles) + " on " + d.stack.toUpperCase() + (S.events[S.sel] === e ? "" : " (most recent before this event)")));
    if (!d.stacked_frame) {
      box.appendChild(el("p", { class: "notice small", text: "Stacking failed: " + (d.stacking_error || "") + ". The frame is lost; SP still moved to " + d.frame_at + "." }));
      return;
    }
    const t = el("table");
    t.appendChild(el("tr", null, el("th", { text: "address" }), el("th", { text: "word" }), el("th", { text: "value" })));
    const names = ["r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr"];
    names.forEach((n, k) => {
      const v = d.stacked_frame[n];
      let note = "";
      if (n === "xpsr") note = " T=" + ((hex(v) >>> 24) & 1) + " IPSR=" + (hex(v) & 0x1ff) + ((hex(v) >>> 9) & 1 ? " +pad" : "");
      if (n === "pc") note = " return address";
      t.appendChild(el("tr", null, el("td", { class: "mono", text: h8(base + 4 * k) }), el("td", { class: "mono", text: n }),
        el("td", { class: "mono", text: v + note })));
    });
    if ((hex(d.stacked_frame.xpsr) >>> 9) & 1) t.appendChild(el("tr", null, el("td", { class: "mono", text: h8(base + 32) }),
      el("td", { class: "mono", text: "(pad)" }), el("td", { class: "small muted", text: "alignment word, removed on return" })));
    box.appendChild(el("div", { class: "scroll-x" }, t));
    box.appendChild(el("p", { class: "small muted", text: "Lowest address = r0 = SP after entry. EXC_RETURN " + d.exc_return + ": " + (EXC_RETURN[d.exc_return] || "") }));
  }

  function renderTaskStacks(box, lay, e, idx) {
    const d = e.details;
    const names = Object.keys(d.stacks);
    if (!S.stackTask || !names.includes(S.stackTask)) S.stackTask = e.from && names.includes(e.from) ? e.from : names[0];
    box.appendChild(el("p", { class: "small" }, "Task stacks as saved at the context switch #" + e.seq + " (cycle " + fmt(e.t_cycles) + ")" +
      (idx === S.sel ? "" : ", the most recent one before this event") + ". " + (e.from || "-") + " was switched out, " + e.to + " in."));
    const tabs = el("div", { class: "tabs", role: "tablist" });
    for (const n of names) {
      tabs.appendChild(el("button", { type: "button", class: n === S.stackTask ? "on" : null, role: "tab",
        onclick: () => { S.stackTask = n; renderStack(); } }, "Task " + n));
    }
    box.appendChild(tabs);
    const snap = d.stacks[S.stackTask];
    const info = lay.tasks[S.stackTask] || {};
    const wm = (d.watermarks || {})[S.stackTask];
    if (wm) {
      box.appendChild(el("div", { class: "small" }, "Watermark: " + wm.used_bytes + " of " + wm.size_bytes + " bytes (" + wm.percent + "%) ever used. Stack " +
        (info.stack_base || "") + " .. " + (info.stack_top || "") + ", guard below at " + (info.guard_base || "")));
      box.appendChild(el("div", { class: "bar" + (wm.percent >= 100 ? " full" : "") }, el("i", { style: "width:" + Math.min(100, wm.percent) + "%" })));
    }
    if (snap.state === "DEAD") {
      box.appendChild(el("p", { class: "notice small", text: "Task " + S.stackTask + " is DEAD; its stack is no longer meaningful." }));
      return;
    }
    const labels = lay.sw_frame.concat(lay.hw_frame);
    const t = el("table");
    t.appendChild(el("tr", null, el("th", { text: "address" }), el("th", { text: "slot" }), el("th", { text: "value" })));
    const psp = hex(snap.psp);
    snap.words.forEach((v, k) => {
      if (k === 0) t.appendChild(el("tr", { class: "grp" }, el("td", { colspan: 3, text: "software frame: pushed by PendSV (" + lay.sw_frame.length + " words)" })));
      if (k === lay.sw_frame.length) t.appendChild(el("tr", { class: "grp" }, el("td", { colspan: 3, text: "hardware frame: pushed by the core on exception entry" })));
      if (k === labels.length) t.appendChild(el("tr", { class: "grp" }, el("td", { colspan: 3, text: "above the frame" })));
      const label = labels[k] || "";
      let note = "";
      if (label === "xpsr") note = " T=" + ((hex(v) >>> 24) & 1) + " IPSR=" + (hex(v) & 0x1ff);
      if (v === lay.watermark_pattern) note = " (never written)";
      t.appendChild(el("tr", { class: k < lay.sw_frame.length ? "sw" : null },
        el("td", { class: "mono", text: h8(psp + 4 * k) + (k === 0 ? " ← PSP" : "") }),
        el("td", { class: "mono", text: label }), el("td", { class: "mono", text: v + note })));
    });
    box.appendChild(el("div", { class: "scroll-x" }, t));
    if (lay.sw_frame.length !== 8) {
      box.appendChild(el("p", { class: "notice small", text: "The software frame has " + lay.sw_frame.length + " words instead of 8 (r4-r11): a callee-saved register is not preserved across switches." }));
    }
  }

  function renderImageMap(box) {
    const im = S.trace.image_map;
    if (!im) return;
    const det = el("details", null, el("summary", { class: "small", text: "Memory map and vector table" }));
    const t = el("table");
    t.appendChild(el("tr", null, el("th", { text: "what" }), el("th", { text: "address" })));
    (im.vectors || []).forEach((v) => t.appendChild(el("tr", null, el("td", { class: "mono", text: "vector[" + v.index + "] " + v.name }),
      el("td", { class: "mono", text: v.value + (v.index > 0 ? ((hex(v.value) & 1) ? "" : "  bit 0 clear!") : "") }))));
    (im.sections || []).forEach((s) => t.appendChild(el("tr", null, el("td", { class: "mono", text: ".text " + s.name + " (" + s.region + ")" }), el("td", { class: "mono", text: s.base }))));
    Object.entries(im.data || {}).forEach(([k, v]) => t.appendChild(el("tr", null, el("td", { class: "mono", text: ".data " + k }), el("td", { class: "mono", text: v }))));
    Object.entries(im.bss || {}).forEach(([k, v]) => t.appendChild(el("tr", null, el("td", { class: "mono", text: ".bss " + k }), el("td", { class: "mono", text: v }))));
    t.appendChild(el("tr", null, el("td", { class: "mono", text: "__data_load (flash copy)" }), el("td", { class: "mono", text: im.data_load })));
    t.appendChild(el("tr", null, el("td", { class: "mono", text: "initial MSP" }), el("td", { class: "mono", text: im.initial_sp })));
    det.appendChild(el("div", { class: "scroll-x" }, t));
    box.appendChild(det);
  }

  // ------------------------------------------------------------------ decoder
  function dl(pairs) {
    const d = el("dl", { class: "kv" });
    for (const [k, v] of pairs) {
      if (v === undefined || v === null || v === "") continue;
      d.appendChild(el("dt", { text: k }));
      d.appendChild(el("dd", { text: String(v) }));
    }
    return d;
  }

  function faultRecord(e) {
    const s = summaryFor(S.variant) || {};
    const idx = e.details && e.details.fault_index;
    return (s.faults && idx !== undefined && s.faults[idx]) || (e.details && e.details.fault);
  }

  function renderFaultRecord(box, f) {
    box.appendChild(el("p", null, el("b", { text: f.kind }), f.escalated_from ? " (escalated from " + f.escalated_from + ")" : "", ": " + f.detail));
    box.appendChild(dl([
      ["faulting PC", f.pc + "  " + f.instruction], ["context", f.context + (f.privileged ? " (privileged)" : " (unprivileged)")],
      ["CFSR", f.cfsr_value], ["HFSR", f.hfsr_value], ["MMFAR", f.mmfar ? f.mmfar + " (valid)" : "not valid"],
      ["BFAR", f.bfar ? f.bfar + " (valid)" : "not valid"], ["stack", f.sp], ["EXC_RETURN", f.exc_return],
    ]));
    const ul = el("ul", { class: "bits" });
    for (const b of f.decoded || []) ul.appendChild(el("li", null, el("b", { text: b.register + "." + b.name }), " (bit " + b.bit + "): " + b.meaning));
    box.appendChild(ul);
    if (f.stacked_frame) {
      box.appendChild(el("p", { class: "small" }, "Stacked frame: " + Object.entries(f.stacked_frame).map(([k, v]) => k + "=" + v).join(" ")));
    } else {
      box.appendChild(el("p", { class: "small muted", text: "No stacked frame: stacking failed, so the stack itself is suspect." }));
    }
  }

  function renderDecoder() {
    const box = $("decoder");
    box.textContent = "";
    const e = S.events[S.sel];
    const d = e.details || {};
    if (e.kind === "fault") {
      const f = faultRecord(e);
      if (f) renderFaultRecord(box, f);
    } else if (["exception_entry", "tail_chain"].includes(e.kind)) {
      box.appendChild(el("p", null, el("b", { text: d.name }), " (exception " + d.exception + ", priority 0x" + (d.priority >>> 0).toString(16).toUpperCase() + ")"));
      box.appendChild(dl([
        ["entered from", e.from], ["vector", d.vector + (hex(d.vector) & 1 ? " (Thumb bit set)" : " (bit 0 CLEAR: INVSTATE)")],
        ["EXC_RETURN", d.exc_return + " : " + (EXC_RETURN[d.exc_return] || "?")],
        ["frame", e.kind === "tail_chain" ? "kept from the previous handler (tail-chain)" : (d.frame_at ? d.frame_at + " on " + d.stack : "")],
        ["IPSR now", d.exception],
      ]));
      if (e.kind === "exception_entry") box.appendChild(el("p", { class: "small muted", text: "Entry costs about 12 cycles here (stacking 8 words and fetching the vector in parallel). A tail-chain costs 6." }));
    } else if (e.kind === "exception_return") {
      box.appendChild(el("p", null, el("b", { text: excName(d.exception) }), " returns"));
      box.appendChild(dl([["EXC_RETURN", d.exc_return + " : " + (EXC_RETURN[d.exc_return] || "")],
        ["result", d.tail_chain_to ? "tail-chain into " + excName(d.tail_chain_to) : "popped from " + d.stack + ", now in " + e.to]]));
      if (d.popped_frame) box.appendChild(el("p", { class: "small" }, "Popped: " + Object.entries(d.popped_frame).map(([k, v]) => k + "=" + v).join(" ")));
    } else if (e.kind === "context_switch") {
      box.appendChild(el("p", null, el("b", { text: (e.from || "-") + " → " + e.to }), " (switch " + (d.switches || "") + ", tick " + (d.tick || 0) + ")"));
      box.appendChild(dl([["saved PSP", d.old_psp], ["next PSP", d.new_psp], ["PendSV EXC_RETURN", d.exc_return ? d.exc_return + " : " + (EXC_RETURN[d.exc_return] || "") : null]]));
      if (d.exc_return && d.exc_return !== "0xFFFFFFFD") box.appendChild(el("p", { class: "notice small", text: "PendSV ran on top of another handler. With PendSV at the lowest priority this never happens." }));
    } else if (e.kind === "syscall" && d.name) {
      box.appendChild(el("p", null, el("b", { text: "svc #" + d.svc + " (" + d.name + ")" }), " from " + e.from));
      box.appendChild(dl([["argument (stacked r0)", d.arg !== undefined ? h8(d.arg) : null], ["return value", d.ret], ["written to", d.return_to === "live_r0" ? "LIVE r0 (lost on return!)" : "stacked r0 at " + (d.frame_at || "")]]));
    } else if (e.kind === "boot_step") {
      box.appendChild(el("p", null, el("b", { text: "Boot step: " + d.step }), d.ok ? " (check passed)" : " (CHECK FAILED)"));
      if (d.problems && d.problems.length) { const ul = el("ul", { class: "bits" }); d.problems.forEach((p) => ul.appendChild(el("li", { text: p }))); box.appendChild(ul); }
      if (d.mismatches && d.mismatches.length) box.appendChild(el("p", { class: "small", text: d.mismatches.join("; ") }));
      if (d.nonzero && d.nonzero.length) box.appendChild(el("p", { class: "small", text: "garbage left in: " + d.nonzero.join(", ") }));
    } else {
      box.appendChild(el("p", { class: "muted small", text: "Select an exception, fault, context switch, system call or boot step to decode it here." }));
    }
    // fault list for the run
    const s = summaryFor(S.variant) || {};
    const faults = s.faults || [];
    const fr = s.fault_registers || {};
    const list = el("div", { class: "faultlist", style: "margin-top:10px" });
    list.appendChild(el("div", { class: "small muted", text: faults.length ? "Faults in this run (" + faults.length + "): CFSR=" + fr.CFSR + " HFSR=" + fr.HFSR + " at the end" : "No faults in this run (CFSR=0x00000000)." }));
    faults.forEach((f, k) => {
      const i = S.events.findIndex((ev) => ev.kind === "fault" && ev.details && ev.details.fault_index === k);
      list.appendChild(el("button", { type: "button", onclick: () => i >= 0 && select(i) }, (k + 1) + ". " + f.kind + " " + f.flag + " @" + fmt(f.t_cycles)));
    });
    box.appendChild(list);
  }

  // --------------------------------------------------------------------- log
  function buildKindFilter() {
    const sel = $("kindFilter");
    const keep = sel.value;
    sel.textContent = "";
    sel.appendChild(el("option", { value: "", text: "all" }));
    const kinds = Array.from(new Set(S.events.map((e) => e.kind))).sort();
    for (const k of kinds) sel.appendChild(el("option", { value: k, text: k }));
    sel.value = kinds.includes(keep) ? keep : "";
  }

  function renderLog() {
    const kind = $("kindFilter").value;
    const q = $("search").value.trim().toLowerCase();
    const body = $("log").querySelector("tbody");
    body.textContent = "";
    let shown = 0;
    const frag = document.createDocumentFragment();
    S.events.forEach((e, i) => {
      if (kind && e.kind !== kind) return;
      const reason = reasonText(e);
      if (q && !(reason + " " + (e.from || "") + " " + (e.to || "") + " " + e.kind + " " + (e.ctx || "")).toLowerCase().includes(q)) return;
      shown++;
      const tr = el("tr", { "data-i": i, onclick: () => select(i) },
        el("td", { class: "num", text: e.seq }), el("td", { class: "num", text: fmt(e.t_cycles) }),
        el("td", { class: "num", text: (e.t_cycles / mhz()).toFixed(2) }),
        el("td", null, el("span", { class: "tag-k " + e.kind, text: e.kind })),
        el("td", { class: "ctx", text: (e.from || "-") + " → " + (e.to || "-") }),
        el("td", { text: reason }));
      frag.appendChild(tr);
    });
    body.appendChild(frag);
    $("logCount").textContent = shown + " of " + S.events.length;
    highlightLogRow(false);
  }
  $("kindFilter").addEventListener("change", renderLog);
  $("search").addEventListener("input", renderLog);

  function highlightLogRow(scroll) {
    const body = $("log").querySelector("tbody");
    const old = body.querySelector("tr.sel");
    if (old) old.classList.remove("sel");
    const row = body.querySelector('tr[data-i="' + S.sel + '"]');
    if (row) {
      row.classList.add("sel");
      if (scroll) {
        const wrap = $("logwrap");
        const top = row.offsetTop - wrap.clientHeight / 2;
        wrap.scrollTop = Math.max(0, top);
      }
    }
  }

  // ------------------------------------------------------------ measurements
  function renderMeasurements() {
    const t = $("measure");
    t.textContent = "";
    const rows = S.trace.summary.measurements || [];
    const variants = S.trace.variants && S.trace.variants.length ? S.trace.variants : [null];
    const keys = [];
    const table = {};
    for (const r of rows) {
      const key = r.name + "|" + r.unit;
      if (!table[key]) { table[key] = { name: r.name, unit: r.unit, note: r.note, vals: {} }; keys.push(key); }
      table[key].vals[r.variant] = r.value;
      if (r.variant === S.variant) table[key].note = r.note;
    }
    const head = el("tr", null, el("th", { text: "measurement" }));
    for (const v of variants) head.appendChild(el("th", { text: v || "value" }));
    head.appendChild(el("th", { text: "unit / note" }));
    t.appendChild(head);
    for (const key of keys) {
      const r = table[key];
      const tr = el("tr", null, el("td", { text: r.name }));
      for (const v of variants) {
        const val = r.vals[v];
        tr.appendChild(el("td", { class: "v", style: v === S.variant && variants.length > 1 ? "font-weight:700" : null, text: val === undefined ? "-" : (typeof val === "number" ? fmt(val) : String(val)) }));
      }
      tr.appendChild(el("td", { class: "small muted", text: (r.unit || "") + (r.note ? "; " + r.note : "") }));
      t.appendChild(tr);
    }
    if (!keys.length) t.appendChild(el("tr", null, el("td", { text: "No measurements for this scenario." })));
  }

  // ------------------------------------------------------------------ result
  function renderResult() {
    const box = $("result");
    box.textContent = "";
    const s = summaryFor(S.variant) || {};
    const checks = S.trace.result.checks.filter((c) => !c.variant || c.variant === S.variant || !S.variant);
    box.appendChild(dl([
      ["stopped", s.stop_reason], ["cycles", fmt(s.cycles) + " (" + s.time_us + " µs)"], ["CPI", s.cpi],
      ["exceptions", (s.exception_entries || 0) + " entries, " + (s.tail_chains || 0) + " tail-chains"],
      ["faults", (s.faults || []).length + (s.lockup ? " (LOCKUP)" : "")],
    ]));
    if (s.kernel) {
      const t = el("table", { style: "margin-top:8px" });
      t.appendChild(el("tr", null, el("th", { text: "task" }), el("th", { text: "state" }), el("th", { text: "run %" }), el("th", { text: "stack used" })));
      for (const [n, k] of Object.entries(s.kernel.tasks)) {
        t.appendChild(el("tr", null, el("td", { class: "mono", text: n }), el("td", { class: "mono", text: k.state }),
          el("td", { class: "mono", text: k.run_percent }), el("td", { class: "mono", text: k.watermark.used_bytes + "/" + k.watermark.size_bytes + " B" })));
      }
      box.appendChild(el("div", { class: "scroll-x" }, t));
    }
    if (s.uart_output) box.appendChild(el("pre", { class: "uart", text: s.uart_output }));
    const ul = el("ul", { class: "checks" });
    for (const c of checks) {
      const args = Object.entries(c.args || {}).map(([k, v]) => k + "=" + (Array.isArray(v) ? "[...]" : v)).join(" ");
      ul.appendChild(el("li", null, el("span", { class: c.passed ? "ok" : "bad", text: c.passed ? "PASS " : "FAIL " }),
        isMystery() ? "(check hidden)" : el("code", { text: (c.variant ? "[" + c.variant + "] " : "") + c.name + " " + args }),
        isMystery() ? "" : " → " + c.detail));
    }
    box.appendChild(el("h4", { class: "small muted", style: "margin:10px 0 2px", text: "Checks against the expected outcome" }));
    box.appendChild(ul);
  }

  // ------------------------------------------------------------------- notes
  function renderNotes() {
    const sc = S.trace.scenario;
    const box = $("notes");
    box.textContent = "";
    $("reveal").classList.toggle("hidden", !isMystery());
    if (isMystery()) {
      box.appendChild(el("p", null, el("b", { text: "Symptom: " }), (sc.variants || []).find((v) => v.name === S.variant && v.hidden_symptom) ?
        sc.variants.find((v) => v.name === S.variant).hidden_symptom : sc.hidden_symptom));
      box.appendChild(el("p", { class: "muted", text: "Root causes, what to look for and the explanation are hidden. Investigate: which contexts run on the timeline, which faults appear in the decoder, what the stacks look like, which boot step fails. Then press Reveal." }));
      return;
    }
    box.appendChild(el("h4", { text: "What to look for" }));
    box.appendChild(el("p", { text: sc.what_to_look_for }));
    box.appendChild(el("h4", { text: "Likely root causes on real hardware" }));
    box.appendChild(el("ul", null, sc.likely_root_causes.map((x) => el("li", { text: x }))));
    box.appendChild(el("h4", { text: "How to debug it" }));
    box.appendChild(el("ol", null, sc.how_to_debug.map((x) => el("li", { text: x }))));
    box.appendChild(el("h4", { text: "What this means" }));
    box.appendChild(el("p", { text: sc.what_this_means }));
  }
  $("reveal").addEventListener("click", () => {
    S.revealed = true;
    renderHeader(); renderNotes(); renderResult(); renderLog(); renderEventLine();
  });
  $("mystery").addEventListener("change", (ev) => {
    S.mystery = ev.target.checked;
    store.set("r2s-mystery", S.mystery ? "1" : "0");
    S.revealed = false;
    if (S.trace) { renderHeader(); renderNotes(); renderResult(); renderLog(); renderEventLine(); }
  });

  // ------------------------------------------------------------------ wiring
  $("scenario").addEventListener("change", (ev) => loadScenario(ev.target.value, null));
  $("variant").addEventListener("change", (ev) => selectVariant(ev.target.value));
  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (S.trace) { const was = S.scale === S.fitScale; if (was) fitTimeline(); } }, 150);
  });
  window.addEventListener("hashchange", () => {
    const [id, v] = decodeURIComponent(location.hash.slice(1)).split(":");
    if (S.trace && id === S.trace.scenario.id && (v || null) === S.variant) return;
    if (S.index && S.index.scenarios.some((s) => s.id === id)) loadScenario(id, v || null);
  });

  init();
})();
