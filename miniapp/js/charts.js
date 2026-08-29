/* researchagen Mini App — движок графиков на чистом canvas.
 * Никаких внешних библиотек: линейные графики с перекрестием по касанию,
 * коридор прогноза, спарклайны. DPR-масштабирование под retina.
 */
"use strict";

const Charts = (() => {
  const registry = new Map(); // id -> LineChart
  const sparks = new Map();   // id -> config

  const fmtK = (x) => (Math.abs(x) >= 1000 ? (x / 1000).toFixed(x >= 10000 ? 0 : 1).replace(/\.0$/, "") + "k" : String(Math.round(x * 10) / 10));

  class LineChart {
    constructor(id, opts) {
      this.id = id;
      this.opts = Object.assign({ padL: 44, padR: 12, padT: 12, padB: 22, logY: false,
        yTicks: 4, xTicks: 4, fmtX: fmtK, fmtY: (v) => (Math.round(v * 100) / 100).toString(),
        series: [], band: null, hlines: [], onScrub: null, gridX: true, minSpanY: 1e-9 }, opts);
      this.canvas = document.getElementById(id);
      if (!this.canvas) return;
      this.ctx = this.canvas.getContext("2d");
      this.hoverX = null;
      this.resize();
      this.canvas.addEventListener("pointerdown", (e) => this.onPointer(e), { passive: true });
      this.canvas.addEventListener("pointermove", (e) => this.onPointer(e), { passive: true });
      this.canvas.addEventListener("pointerleave", () => { this.hoverX = null; this.draw(); this.emitScrub(null); }, { passive: true });
      window.addEventListener("resize", () => { this.resize(); });
    }

    resize() {
      const c = this.canvas;
      if (!c || !c.parentElement) return;
      const dpr = Math.min(2.5, window.devicePixelRatio || 1);
      const w = c.parentElement.clientWidth;
      const h = this.opts.height || c.parentElement.clientHeight || 190;
      c.width = Math.max(10, Math.round(w * dpr));
      c.height = Math.round(h * dpr);
      c.style.width = w + "px";
      c.style.height = h + "px";
      this.dpr = dpr;
      this.W = w; this.H = h;
      this.draw();
    }

    update(opts) {
      Object.assign(this.opts, opts || {});
      this.draw();
    }

    bounds() {
      const sers = this.opts.series.filter((s) => s.data && s.data.length);
      let xs = [], ys = [];
      for (const s of sers) for (const p of s.data) { xs.push(p[0]); ys.push(p[1]); }
      if (!xs.length) return null;
      let xmin = Math.min(...xs), xmax = Math.max(...xs);
      if (xmax - xmin < 1e-9) xmax = xmin + 1;
      let ymin = Math.min(...ys), ymax = Math.max(...ys);
      const band = this.opts.band;
      if (band && band.to != null) { ymin = Math.min(ymin, band.to); ymax = Math.max(ymax, band.to); }
      if (ymax - ymin < this.opts.minSpanY) ymax = ymin + this.opts.minSpanY;
      const padY = (ymax - ymin) * 0.1;
      ymin -= padY; ymax += padY;
      if (this.opts.logY) { ymin = Math.max(1e-6, ymin); }
      return { xmin, xmax, ymin, ymax };
    }

    sx(x, b) { return this.opts.padL + (x - b.xmin) / (b.xmax - b.xmin) * (this.W - this.opts.padL - this.opts.padR); }
    sy(y, b) {
      let t;
      if (this.opts.logY) {
        const ly0 = Math.log10(Math.max(1e-6, b.ymin)), ly1 = Math.log10(Math.max(1e-6, b.ymax));
        t = (Math.log10(Math.max(1e-6, y)) - ly0) / (ly1 - ly0 || 1);
      } else t = (y - b.ymin) / (b.ymax - b.ymin);
      return this.H - this.opts.padB - t * (this.H - this.opts.padT - this.opts.padB);
    }

    draw() {
      const c = this.canvas, ctx = this.ctx;
      if (!c || !ctx) return;
      ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      ctx.clearRect(0, 0, this.W, this.H);
      const css = getComputedStyle(document.body);
      const colGrid = css.getPropertyValue("--ch-grid").trim() || "rgba(255,255,255,.07)";
      const colTx = css.getPropertyValue("--ch-tx").trim() || "rgba(255,255,255,.45)";
      const b = this.bounds();
      const band = this.opts.band;
      if (!b) { // пусто — заглушка
        ctx.fillStyle = colTx; ctx.font = "13px system-ui"; ctx.textAlign = "center";
        ctx.fillText("нет данных", this.W / 2, this.H / 2);
        return;
      }
      // сетка Y
      ctx.font = "11.5px ui-monospace, SFMono-Regular, Menlo, monospace";
      ctx.textAlign = "right"; ctx.textBaseline = "middle";
      for (let i = 0; i <= this.opts.yTicks; i++) {
        let y;
        if (this.opts.logY) {
          const ly0 = Math.log10(Math.max(1e-6, b.ymin)), ly1 = Math.log10(Math.max(1e-6, b.ymax));
          y = Math.pow(10, ly0 + (ly1 - ly0) * i / this.opts.yTicks);
        } else y = b.ymin + (b.ymax - b.ymin) * i / this.opts.yTicks;
        const py = this.sy(y, b);
        ctx.strokeStyle = colGrid; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(this.opts.padL, py); ctx.lineTo(this.W - this.opts.padR, py); ctx.stroke();
        ctx.fillStyle = colTx;
        ctx.fillText(this.opts.fmtY(y), this.opts.padL - 6, py);
      }
      // сетка X
      if (this.opts.gridX) {
        ctx.textAlign = "center"; ctx.textBaseline = "top";
        for (let i = 0; i <= this.opts.xTicks; i++) {
          const x = b.xmin + (b.xmax - b.xmin) * i / this.opts.xTicks;
          const px = this.sx(x, b);
          ctx.strokeStyle = colGrid; ctx.beginPath(); ctx.moveTo(px, this.opts.padT); ctx.lineTo(px, this.H - this.opts.padB); ctx.stroke();
          ctx.fillStyle = colTx;
          ctx.fillText(this.opts.fmtX(x), px, this.H - this.opts.padB + 5);
        }
      }
      // коридор прогноза
      if (band && band.from != null && band.to != null) {
        const y1 = this.sy(band.from, b), y2 = this.sy(band.to, b);
        ctx.fillStyle = band.color || "rgba(124,108,255,.14)";
        ctx.fillRect(this.opts.padL, Math.min(y1, y2), this.W - this.opts.padL - this.opts.padR, Math.abs(y2 - y1));
        if (band.label) {
          ctx.fillStyle = css.getPropertyValue("--ch-band").trim() || "rgba(124,108,255,.8)";
          ctx.textAlign = "left"; ctx.textBaseline = "bottom";
          ctx.font = "11px system-ui";
          ctx.fillText(band.label, this.opts.padL + 6, Math.min(y1, y2) - 2);
        }
      }
      // горизонтальные линии-ориентиры
      for (const hl of this.opts.hlines || []) {
        const py = this.sy(hl.y, b);
        ctx.strokeStyle = hl.color || colGrid; ctx.setLineDash(hl.dash || [4, 4]); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(this.opts.padL, py); ctx.lineTo(this.W - this.opts.padR, py); ctx.stroke();
        ctx.setLineDash([]);
        if (hl.label) {
          ctx.fillStyle = hl.color || colTx; ctx.font = "11px system-ui"; ctx.textAlign = "right"; ctx.textBaseline = "bottom";
          ctx.fillText(hl.label, this.W - this.opts.padR - 4, py - 2);
        }
      }
      // серии
      for (const s of this.opts.series) {
        if (!s.data || !s.data.length) continue;
        ctx.strokeStyle = s.color; ctx.lineWidth = s.width || 2;
        ctx.setLineDash(s.dash || []);
        ctx.lineJoin = "round"; ctx.lineCap = "round";
        if (s.fill) {
          ctx.beginPath();
          ctx.moveTo(this.sx(s.data[0][0], b), this.sy(s.data[0][1], b));
          for (const p of s.data) ctx.lineTo(this.sx(p[0], b), this.sy(p[1], b));
          ctx.lineTo(this.sx(s.data[s.data.length - 1][0], b), this.H - this.opts.padB);
          ctx.lineTo(this.sx(s.data[0][0], b), this.H - this.opts.padB);
          ctx.closePath();
          const g = ctx.createLinearGradient(0, this.opts.padT, 0, this.H - this.opts.padB);
          g.addColorStop(0, s.fill); g.addColorStop(1, "rgba(0,0,0,0)");
          ctx.fillStyle = g; ctx.fill();
        }
        ctx.beginPath();
        ctx.moveTo(this.sx(s.data[0][0], b), this.sy(s.data[0][1], b));
        for (const p of s.data) ctx.lineTo(this.sx(p[0], b), this.sy(p[1], b));
        ctx.stroke();
        ctx.setLineDash([]);
        if (s.dotLast !== false && s.data.length) { // живая точка
          const last = s.data[s.data.length - 1];
          const px = this.sx(last[0], b), py = this.sy(last[1], b);
          ctx.fillStyle = s.color;
          ctx.beginPath(); ctx.arc(px, py, 3, 0, Math.PI * 2); ctx.fill();
        }
      }
      // перекрестие
      if (this.hoverX != null) {
        ctx.strokeStyle = css.getPropertyValue("--ch-cross").trim() || "rgba(255,255,255,.5)";
        ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(this.hoverX, this.opts.padT); ctx.lineTo(this.hoverX, this.H - this.opts.padB); ctx.stroke();
        ctx.setLineDash([]);
        for (const s of this.opts.series) {
          if (!s.data || !s.data.length) continue;
          const p = this.nearest(s.data, this.hoverX, b);
          if (!p) continue;
          const px = this.sx(p[0], b), py = this.sy(p[1], b);
          ctx.fillStyle = s.color;
          ctx.beginPath(); ctx.arc(px, py, 4, 0, Math.PI * 2); ctx.fill();
          ctx.strokeStyle = "rgba(0,0,0,.5)"; ctx.lineWidth = 1.5; ctx.stroke();
        }
      }
    }

    nearest(data, px, b) {
      let best = null, bd = Infinity;
      for (const p of data) {
        const d = Math.abs(this.sx(p[0], b) - px);
        if (d < bd) { bd = d; best = p; }
      }
      return bd < 30 ? best : null;
    }

    onPointer(e) {
      const r = this.canvas.getBoundingClientRect();
      this.hoverX = e.clientX - r.left;
      this.draw();
      this.emitScrub(this.hoverX);
    }

    emitScrub(px) {
      if (!this.opts.onScrub) return;
      if (px == null) { this.opts.onScrub(null); return; }
      const b = this.bounds();
      if (!b) return this.opts.onScrub(null);
      const out = {};
      for (const s of this.opts.series) {
        const p = s.data && this.nearest(s.data, px, b);
        if (p) out[s.id] = { x: p[0], y: p[1] };
      }
      if (Object.keys(out).length) this.opts.onScrub(out); else this.opts.onScrub(null);
    }
  }

  function line(id, opts) {
    let ch = registry.get(id);
    const el = document.getElementById(id);
    if (!el) return null;
    if (!ch || ch.canvas !== el) {
      ch = new LineChart(id, opts);
      registry.set(id, ch);
    } else ch.update(opts);
    return ch;
  }

  function drop(id) { registry.delete(id); }

  function sparkline(id, data, color, opts = {}) {
    const c = document.getElementById(id);
    if (!c || !data || !data.length) return;
    const dpr = Math.min(2.5, window.devicePixelRatio || 1);
    const w = c.parentElement.clientWidth || 64, h = opts.height || 26;
    c.width = w * dpr; c.height = h * dpr; c.style.width = w + "px"; c.style.height = h + "px";
    const ctx = c.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const ys = data.map((p) => (Array.isArray(p) ? p[1] : p));
    let ymin = Math.min(...ys), ymax = Math.max(...ys);
    if (ymax - ymin < 1e-9) ymax = ymin + 1;
    const X = (i) => 1 + i / (ys.length - 1 || 1) * (w - 2);
    const Y = (y) => h - 2 - (y - ymin) / (ymax - ymin) * (h - 4);
    ctx.beginPath();
    ys.forEach((y, i) => (i ? ctx.lineTo(X(i), Y(y)) : ctx.moveTo(X(i), Y(y))));
    ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.lineJoin = "round"; ctx.stroke();
    if (opts.fill) {
      ctx.lineTo(X(ys.length - 1), h); ctx.lineTo(X(0), h); ctx.closePath();
      const g = ctx.createLinearGradient(0, 0, 0, h);
      g.addColorStop(0, color.replace(/[\d.]+\)$/, "0.22)")); g.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = g; ctx.fill();
    }
  }

  return { line, drop, sparkline, fmtK };
})();

if (typeof window !== "undefined") window.Charts = Charts;
