/* ============================================================
   charts.js — графики на canvas без зависимостей (DPR-aware)
   LineChart / Sparkline / Ring / Donut
   ============================================================ */
"use strict";

const Charts = (() => {

  function cssVar(name, fallback) {
    try {
      const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (e) { return fallback; }
  }
  function palette() {
    return {
      text: cssVar("--text-2", "#b9c3d8"),
      faint: cssVar("--faint", "#586180"),
      grid: cssVar("--border", "rgba(148,163,196,.11)"),
      surface: cssVar("--surface", "#121826"),
      accent: cssVar("--accent", "#4fc3ff"),
      violet: cssVar("--violet", "#8b7cff"),
      ok: cssVar("--ok", "#3be0a0"),
      warn: cssVar("--warn", "#ffb437"),
      danger: cssVar("--danger", "#ff6161"),
      mono: cssVar("--mono", "monospace"),
    };
  }

  /* ---------- LineChart ---------- */
  class LineChart {
    /**
     * canvas — элемент canvas
     * opts: { yFmt, xFmt, yMin, yMax, log, gridX (число линий), tooltip(el), series: {id:{color,fill,dash}} }
     */
    constructor(canvas, opts = {}) {
      this.cv = canvas;
      this.ctx = canvas.getContext("2d");
      this.opts = Object.assign({ yFmt: v => (Math.round(v * 100) / 100), xFmt: null, gridY: 4, gridX: 4, log: false, window: 0 }, opts);
      this.series = [];           // [{id,name,color,points:[{x,y}],hidden}]
      this.tip = opts.tooltip || null;
      this.crossI = -1;
      this._bind();
      this.resize();
    }
    setData(list) {
      this.series = list.map(s => Object.assign({ points: [], hidden: false }, s));
      this.draw();
    }
    append(id, pt) {
      const s = this.series.find(x => x.id === id);
      if (!s) return;
      s.points.push(pt);
      if (this.opts.window && s.points.length > this.opts.window) s.points = s.points.slice(-this.opts.window);
      this.draw();
    }
    trimLast(id) { const s = this.series.find(x => x.id === id); if (s) { s.points.pop(); this.draw(); } }
    setLog(on) { this.opts.log = on; this.draw(); }
    toggle(id) {
      const s = this.series.find(x => x.id === id);
      if (s) { s.hidden = !s.hidden; this.draw(); }
      return s ? !s.hidden : false;
    }
    visible() { return this.series.filter(s => !s.hidden); }

    resize() {
      const r = this.cv.getBoundingClientRect();
      if (!r.width) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
      this.w = r.width; this.h = r.height;
      this.cv.width = Math.round(r.width * dpr);
      this.cv.height = Math.round(r.height * dpr);
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this.draw();
    }

    _range() {
      const vis = this.visible();
      let xs = [], ys = [];
      vis.forEach(s => s.points.forEach(p => { xs.push(p.x); ys.push(p.y); }));
      if (!xs.length) return null;
      let xmin = Math.min(...xs), xmax = Math.max(...xs);
      if (xmin === xmax) { xmin -= 1; xmax += 1; }
      let ymin = this.opts.yMin != null ? this.opts.yMin : Math.min(...ys);
      let ymax = this.opts.yMax != null ? this.opts.yMax : Math.max(...ys);
      if (this.opts.log) { ymin = Math.max(1e-9, ymin); if (ymax <= 0) ymax = 1; }
      if (!this.opts.yMin != null && ymax === ymin) { ymax = ymin + Math.max(Math.abs(ymin) * .2, .5); ymin -= Math.max(Math.abs(ymin) * .1, .1); }
      return { xmin, xmax, ymin, ymax };
    }
    _sy(v, rg) {
      if (this.opts.log) {
        const lv = Math.log10(Math.max(v, 1e-9)), lo = Math.log10(Math.max(rg.ymin, 1e-9)), hi = Math.log10(rg.ymax);
        return this.pad.t + (1 - (lv - lo) / (hi - lo || 1)) * (this.h - this.pad.t - this.pad.b);
      }
      return this.pad.t + (1 - (v - rg.ymin) / (rg.ymax - rg.ymin || 1)) * (this.h - this.pad.t - this.pad.b);
    }
    _sx(v, rg) { return this.pad.l + (v - rg.xmin) / (rg.xmax - rg.xmin || 1) * (this.w - this.pad.l - this.pad.r); }

    draw() {
      if (!this.w) { this.resize(); return; }
      const c = this.ctx, P = palette();
      this.pad = { l: 46, r: 12, t: 12, b: 22 };
      c.clearRect(0, 0, this.w, this.h);
      const rg = this._range();
      if (!rg) return;

      // сетка
      c.font = "10px " + P.mono; c.fillStyle = P.faint; c.strokeStyle = P.grid;
      c.lineWidth = 1;
      for (let i = 0; i <= this.opts.gridY; i++) {
        const v = rg.ymin + (rg.ymax - rg.ymin) * i / this.opts.gridY;
        const y = Math.round(this._sy(v, rg)) + .5;
        c.beginPath(); c.moveTo(this.pad.l, y); c.lineTo(this.w - this.pad.r, y); c.stroke();
        c.fillText(this.opts.yFmt(v), 6, y + 3);
      }
      for (let i = 0; i <= this.opts.gridX; i++) {
        const v = rg.xmin + (rg.xmax - rg.xmin) * i / this.opts.gridX;
        const x = Math.round(this._sx(v, rg)) + .5;
        c.strokeStyle = P.grid;
        c.beginPath(); c.moveTo(x, this.pad.t); c.lineTo(x, this.h - this.pad.b); c.stroke();
        if (this.opts.xFmt) { c.fillStyle = P.faint; const t = this.opts.xFmt(v); c.textAlign = "center"; c.fillText(t, x, this.h - 7); c.textAlign = "left"; }
      }

      // серии
      const vis = this.visible();
      vis.forEach((s, si) => {
        const pts = s.points;
        if (pts.length < 1) return;
        c.strokeStyle = s.color; c.lineWidth = s.width || 2;
        c.setLineDash(s.dash || []);
        c.lineJoin = "round"; c.lineCap = "round";
        c.beginPath();
        pts.forEach((p, i) => {
          const x = this._sx(p.x, rg), y = this._sy(p.y, rg);
          i ? c.lineTo(x, y) : c.moveTo(x, y);
        });
        c.stroke();
        c.setLineDash([]);
        // заливка под первой видимой серией
        if (si === 0 && s.fill !== false && pts.length > 1) {
          const g = c.createLinearGradient(0, this.pad.t, 0, this.h - this.pad.b);
          g.addColorStop(0, s.color + "3d"); g.addColorStop(1, s.color + "00");
          c.lineTo(this._sx(pts[pts.length - 1].x, rg), this.h - this.pad.b);
          c.lineTo(this._sx(pts[0].x, rg), this.h - this.pad.b);
          c.closePath(); c.fillStyle = g; c.fill();
        }
        // последняя точка
        const lp = pts[pts.length - 1];
        const lx = this._sx(lp.x, rg), ly = this._sy(lp.y, rg);
        c.beginPath(); c.arc(lx, ly, 3.4, 0, 7); c.fillStyle = s.color; c.fill();
        c.beginPath(); c.arc(lx, ly, 7, 0, 7); c.fillStyle = s.color + "33"; c.fill();
      });

      // прицел
      if (this.crossI >= 0 && vis.length) {
        const ref = vis[0];
        if (this.crossI < ref.points.length) {
          const px = this._sx(ref.points[this.crossI].x, rg);
          c.strokeStyle = P.faint + "aa"; c.setLineDash([4, 4]); c.lineWidth = 1;
          c.beginPath(); c.moveTo(px, this.pad.t); c.lineTo(px, this.h - this.pad.b); c.stroke();
          c.setLineDash([]);
          vis.forEach(s => {
            if (this.crossI >= s.points.length) return;
            const p = s.points[this.crossI];
            const x = this._sx(p.x, rg), y = this._sy(p.y, rg);
            c.beginPath(); c.arc(x, y, 4.5, 0, 7); c.fillStyle = s.color; c.fill();
            c.lineWidth = 2; c.strokeStyle = P.surface; c.stroke();
          });
        }
      }
    }

    _bind() {
      const move = (clientX) => {
        if (!this.series.length || !this.w) return;
        const r = this.cv.getBoundingClientRect();
        const rg = this._range(); if (!rg) return;
        const mx = clientX - r.left;
        const vis = this.visible(); if (!vis.length) return;
        const ref = vis[0]; const n = ref.points.length; if (!n) return;
        const t = (mx - this.pad.l) / (this.w - this.pad.l - this.pad.r);
        let i = Math.round(t * (n - 1));
        i = Math.max(0, Math.min(n - 1, i));
        this.crossI = i; this.draw();
        if (this.tip) {
          const lines = [`шаг <b style="color:${palette().text}">${this.opts.xFmt ? this.opts.xFmt(ref.points[i].x) : Math.round(ref.points[i].x)}</b>`];
          vis.forEach(s => {
            if (i < s.points.length) lines.push(`<span class="t-name"><i class="t-dot" style="background:${s.color}"></i>${s.name} · <b>${this.opts.yFmt(s.points[i].y)}</b></span>`);
          });
          this.tip.innerHTML = lines.join("<br>");
          this.tip.classList.add("on");
          const bx = this._sx(ref.points[i].x, rg);
          const tw = this.tip.offsetWidth;
          this.tip.style.left = Math.max(4, Math.min(this.w - tw - 4, bx - tw / 2)) + "px";
          this.tip.style.top = "6px";
        }
      };
      const end = () => { this.crossI = -1; this.draw(); if (this.tip) this.tip.classList.remove("on"); };
      this.cv.addEventListener("touchstart", e => { if (e.touches.length === 1) move(e.touches[0].clientX); }, { passive: true });
      this.cv.addEventListener("touchmove", e => { if (e.touches.length === 1) { move(e.touches[0].clientX); e.preventDefault(); } }, { passive: false });
      this.cv.addEventListener("touchend", end);
      this.cv.addEventListener("mousedown", e => move(e.clientX));
      this.cv.addEventListener("mousemove", e => { if (e.buttons) move(e.clientX); });
      this.cv.addEventListener("mouseleave", end);
    }
  }

  /* ---------- Sparkline ---------- */
  function spark(canvas, data, color, opts = {}) {
    const r = canvas.getBoundingClientRect();
    if (!r.width) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
    canvas.width = r.width * dpr; canvas.height = r.height * dpr;
    const c = canvas.getContext("2d");
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    const w = r.width, h = r.height;
    c.clearRect(0, 0, w, h);
    if (!data || data.length < 2) return;
    let mn = Math.min(...data), mx = Math.max(...data);
    if (opts.min != null) mn = opts.min;
    if (opts.max != null) mx = opts.max;
    if (mx === mn) mx = mn + 1;
    const X = i => i / (data.length - 1) * (w - 2) + 1;
    const Y = v => h - 2 - (v - mn) / (mx - mn) * (h - 5);
    if (opts.fill !== false) {
      const g = c.createLinearGradient(0, 0, 0, h);
      g.addColorStop(0, color + "44"); g.addColorStop(1, color + "00");
      c.beginPath();
      data.forEach((v, i) => i ? c.lineTo(X(i), Y(v)) : c.moveTo(X(i), Y(v)));
      c.lineTo(w - 1, h); c.lineTo(1, h); c.closePath(); c.fillStyle = g; c.fill();
    }
    c.beginPath();
    data.forEach((v, i) => i ? c.lineTo(X(i), Y(v)) : c.moveTo(X(i), Y(v)));
    c.strokeStyle = color; c.lineWidth = 1.8; c.lineJoin = "round"; c.stroke();
    if (opts.dot !== false) { c.beginPath(); c.arc(X(data.length - 1), Y(data[data.length - 1]), 2.2, 0, 7); c.fillStyle = color; c.fill(); }
  }

  /* ---------- Кольцевой индикатор (SVG) ---------- */
  let gradInstalled = false;
  function installGrad() {
    if (gradInstalled) return;
    gradInstalled = true;
    const NS = "http://www.w3.org/2000/svg";
    const defs = document.createElementNS(NS, "defs");
    defs.innerHTML = `<linearGradient id="ringGrad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#4fc3ff"/><stop offset="100%" stop-color="#8b7cff"/></linearGradient>`;
    document.body.appendChild(defs);
  }
  function ring(mount, { size = 112, stroke = 9, value = 0, max = 100, cls = "" }) {
    installGrad();
    const r = (size - stroke) / 2, circ = 2 * Math.PI * r;
    mount.classList.add("ring");
    mount.innerHTML = `
      <svg viewBox="0 0 ${size} ${size}">
        <circle class="trk" cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke-width="${stroke}"/>
        <circle class="val" cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke-width="${stroke}"
          stroke-dasharray="${circ}" stroke-dashoffset="${circ}"/>
      </svg>
      <div class="ring-c ${cls}"></div>`;
    const el = mount.querySelector(".val");
    const api = { set(v, numEl, lbl) {
      const frac = Math.max(0, Math.min(1, v / (max || 1)));
      el.style.strokeDashoffset = circ * (1 - frac);
      if (numEl) numEl.textContent = v;
    }};
    api._circ = circ;
    return api;
  }

  /* ---------- Donut ---------- */
  function donut(mount, segs) {
    // segs: [{value, color, label}]
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 96 96");
    const r = 40, circ = 2 * Math.PI * r;
    const total = segs.reduce((a, s) => a + s.value, 0) || 1;
    let acc = 0;
    segs.forEach(s => {
      const c = document.createElementNS(NS, "circle");
      c.setAttribute("cx", 48); c.setAttribute("cy", 48); c.setAttribute("r", r);
      c.setAttribute("fill", "none"); c.setAttribute("stroke-width", 10);
      c.setAttribute("stroke", s.color);
      c.setAttribute("stroke-dasharray", `${circ * s.value / total - 2.5} ${circ}`);
      c.setAttribute("stroke-dashoffset", -circ * acc / total);
      c.setAttribute("stroke-linecap", "round");
      svg.appendChild(c);
      acc += s.value;
    });
    mount.innerHTML = "";
    mount.appendChild(svg);
  }

  return { LineChart, spark, ring, donut, palette };
})();
