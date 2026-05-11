/* ===========================================================
   STRATOSYN · FINALS REVIEW WEEK · SHARED JAVASCRIPT
   ===========================================================
   Provides:
     Stratosyn.Logo       — animated 3×3 brand mark fill levels
     Stratosyn.Worksheet  — MC selection, fill-in tracking,
                            time-per-question via IntersectionObserver,
                            assemble-and-copy submission payload
     Stratosyn.Board      — slide engine (next/prev, scene strip,
                            keyboard nav, HUD progress)
   =========================================================== */

(function (root) {
  'use strict';

  const Stratosyn = root.Stratosyn = root.Stratosyn || {};

  /* ------------------ small utilities ------------------ */
  const $  = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const fmtTime = ms => {
    const s = Math.round(ms / 1000);
    if (s < 60) return s + 's';
    const m = Math.floor(s / 60);
    const r = s % 60;
    return m + 'm ' + (r < 10 ? '0' : '') + r + 's';
  };
  const padQ = i => 'Q' + String(i).padStart(2, ' ');

  /* ===========================================================
     LOGO — fill level reflects completion progress
     Level 0 = blank, 1 = bottom row, 2 = bottom+middle, 3 = full
     =========================================================== */
  Stratosyn.Logo = {
    setLevel(level) {
      $$('.bm').forEach(bm => bm.dataset.fill = String(level));
    },
    setFromRatio(ratio) {
      let lvl = 0;
      if (ratio >= 0.99) lvl = 3;
      else if (ratio >= 0.66) lvl = 2;
      else if (ratio >= 0.33) lvl = 1;
      else lvl = 0;
      this.setLevel(lvl);
    }
  };

  /* ===========================================================
     WORKSHEET ENGINE
     =========================================================== */
  Stratosyn.Worksheet = {
    _config: null,
    _start: 0,
    _timers: {},        // { qid: { totalMs, lastEnter, answered } }
    _activeQ: null,
    _observer: null,

    init(config) {
      this._config = config || {};
      this._start = performance.now();
      this._initTimers();
      this._wireOptions();
      this._wireFillIns();
      this._wireTextareas();
      this._wireProgressTracking();
      this._wireScrollObserver();
      this._wireProgressBar();
      this._wireScrollTop();
      this._wireCopy();
      this._wireInfoFields();
      this._updateProgress();
    },

    /* ---------- per-question timers ---------- */
    _initTimers() {
      $$('.question').forEach(q => {
        const id = q.id;
        if (!id) return;
        this._timers[id] = { totalMs: 0, lastEnter: null, answered: false };
      });
    },

    _wireScrollObserver() {
      // Mark question as "active" when ≥50% of it is visible.
      // Track time accumulated while active.
      this._observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
          const id = entry.target.id;
          const t  = this._timers[id];
          if (!t) return;
          if (entry.isIntersecting && entry.intersectionRatio >= 0.5) {
            // entered active state
            if (!t.lastEnter) {
              t.lastEnter = performance.now();
              entry.target.classList.add('active-view');
              this._activeQ = id;
              this._updateLiveTime(entry.target, t.totalMs);
            }
          } else {
            // exited active state — flush time
            if (t.lastEnter) {
              t.totalMs += performance.now() - t.lastEnter;
              t.lastEnter = null;
              entry.target.classList.remove('active-view');
              this._updateLiveTime(entry.target, t.totalMs);
            }
          }
        });
      }, { threshold: [0, 0.5, 1.0] });
      $$('.question').forEach(q => this._observer.observe(q));
      // Live tick for the currently-active question's display
      setInterval(() => {
        if (!this._activeQ) return;
        const el = document.getElementById(this._activeQ);
        const t  = this._timers[this._activeQ];
        if (el && t && t.lastEnter) {
          this._updateLiveTime(el, t.totalMs + (performance.now() - t.lastEnter));
        }
      }, 1000);
    },

    _updateLiveTime(qEl, ms) {
      let badge = qEl.querySelector('.q-time');
      if (!badge) {
        badge = document.createElement('div');
        badge.className = 'q-time';
        qEl.appendChild(badge);
      }
      badge.textContent = fmtTime(ms);
    },

    _flushActive() {
      const id = this._activeQ;
      if (!id) return;
      const t = this._timers[id];
      if (t && t.lastEnter) {
        t.totalMs += performance.now() - t.lastEnter;
        t.lastEnter = null;
      }
    },

    /* ---------- multiple choice ---------- */
    _wireOptions() {
      $$('.option').forEach(btn => {
        btn.addEventListener('click', () => {
          const wrap = btn.closest('.options');
          if (!wrap) return;
          $$('.option', wrap).forEach(o => o.classList.remove('selected'));
          btn.classList.add('selected');
          const q = btn.closest('.question');
          if (q) {
            this._timers[q.id].answered = true;
            this._updateProgress();
          }
        });
      });
    },

    /* ---------- fill-in inputs ---------- */
    _wireFillIns() {
      $$('.fill-input').forEach(inp => {
        inp.addEventListener('input', () => {
          const q = inp.closest('.question');
          if (q) {
            this._timers[q.id].answered = inp.value.trim().length > 0;
            this._updateProgress();
          }
        });
      });
    },

    /* ---------- short-answer textareas ---------- */
    _wireTextareas() {
      $$('.frq-textarea').forEach(ta => {
        ta.addEventListener('input', () => {
          const q = ta.closest('.question');
          if (q) {
            this._timers[q.id].answered = ta.value.trim().length > 0;
            this._updateProgress();
          }
        });
      });
    },

    /* ---------- name/period field tracking ---------- */
    _wireInfoFields() {
      $$('.si-input').forEach(inp => {
        inp.addEventListener('input', () => this._updateProgress());
      });
    },

    /* ---------- completion ratio drives logo + stats ---------- */
    _updateProgress() {
      const total = Object.keys(this._timers).length;
      const answered = Object.values(this._timers).filter(t => t.answered).length;
      const ratio = total === 0 ? 0 : answered / total;
      Stratosyn.Logo.setFromRatio(ratio);

      const cAnswered = $('#stat-answered');
      if (cAnswered) cAnswered.textContent = answered + ' / ' + total;
      const cElapsed = $('#stat-elapsed');
      if (cElapsed) cElapsed.textContent = fmtTime(performance.now() - this._start);
      const cAvg = $('#stat-avg');
      if (cAvg) {
        const totalAns = Object.values(this._timers).reduce((s, t) => s + t.totalMs, 0);
        const avg = answered > 0 ? totalAns / answered : 0;
        cAvg.textContent = answered > 0 ? fmtTime(avg) : '—';
      }
    },

    _wireProgressTracking() {
      // Refresh elapsed every second
      setInterval(() => this._updateProgress(), 1000);
    },

    /* ---------- top progress bar + scroll-to-top ---------- */
    _wireProgressBar() {
      const bar = $('#progressBar');
      if (!bar) return;
      window.addEventListener('scroll', () => {
        const scrolled = window.scrollY;
        const total = document.documentElement.scrollHeight - window.innerHeight;
        const pct = total > 0 ? scrolled / total : 0;
        bar.style.transform = 'scaleX(' + pct + ')';
      });
    },
    _wireScrollTop() {
      const st = $('#scrollTop');
      if (!st) return;
      window.addEventListener('scroll', () => {
        if (window.scrollY > 300) st.classList.add('visible');
        else st.classList.remove('visible');
      });
    },

    /* ---------- BUILD COPY PAYLOAD ---------- */
    _buildPayload() {
      this._flushActive();
      const cfg = this._config;
      const fields = ['studentName', 'studentPeriod', 'studentDate'];
      const get = id => {
        const el = document.getElementById(id);
        return (el && el.value.trim()) || '[not entered]';
      };
      const name   = get('studentName');
      const period = get('studentPeriod');
      const date   = get('studentDate');

      const elapsed = performance.now() - this._start;
      const total = Object.keys(this._timers).length;
      const answered = Object.values(this._timers).filter(t => t.answered).length;
      const totalQ = Object.values(this._timers).reduce((s, t) => s + t.totalMs, 0);

      let out = '';
      out += '═════════════════════════════════════════\n';
      out += '  STRATOSYN · ' + (cfg.title || 'WORKSHEET') + '\n';
      out += '  Ms. Madewell · Room 501 · ALA Vistancia\n';
      out += '  Finals Review Week · Day ' + (cfg.day || 'N') + '\n';
      out += '═════════════════════════════════════════\n';
      out += 'Name:        ' + name + '\n';
      out += 'Period:      ' + period + '\n';
      out += 'Date:        ' + date + '\n';
      out += 'Submitted:   ' + new Date().toLocaleString('en-US') + '\n';
      out += '─────────────────────────────────────────\n';
      out += 'Total time:        ' + fmtTime(elapsed) + '\n';
      out += 'Questions answered:' + ' ' + answered + ' / ' + total + '\n';
      out += 'Total time on Qs:  ' + fmtTime(totalQ) + '\n';
      out += '─────────────────────────────────────────\n\n';

      // Walk DOM in order to preserve question order regardless of types
      const allQ = $$('.question');
      allQ.forEach((q, idx) => {
        const id = q.id;
        const t  = this._timers[id] || { totalMs: 0, answered: false };
        const tag = q.querySelector('.q-tag')?.textContent.trim() || 'Question';
        const num = q.querySelector('.q-num')?.textContent.trim() || ('Question ' + (idx + 1));
        out += '── ' + num + ' · ' + tag + ' ──\n';
        out += 'Time on question: ' + fmtTime(t.totalMs) + '\n';

        // Determine question type by what input it has
        const sel = q.querySelector('.option.selected');
        const fill = q.querySelector('.fill-input');
        const ta   = q.querySelector('.frq-textarea');

        if (sel) {
          const letter = sel.dataset.letter || '?';
          const text   = sel.querySelector('.opt-text')?.textContent.trim() || '';
          out += 'Answer: ' + letter + ' — ' + text + '\n';
        } else if (fill) {
          const v = fill.value.trim() || '[blank]';
          out += 'Answer: ' + v + '\n';
        } else if (ta) {
          const v = ta.value.trim() || '[blank]';
          out += 'Response:\n' + v + '\n';
        } else {
          out += 'Answer: [no answer recorded]\n';
        }
        out += '\n';
      });

      out += '═════════════════════════════════════════\n';
      out += '  END SUBMISSION · Day ' + (cfg.day || 'N') + '\n';
      out += '═════════════════════════════════════════\n';
      return out;
    },

    /* ---------- COPY BUTTON ---------- */
    _wireCopy() {
      const btn = $('#copyBtn');
      if (!btn) return;
      btn.addEventListener('click', async () => {
        const txt = this._buildPayload();
        const status = $('#copyStatus');
        const preview = $('#copyPreview');
        const btnLabel = $('#copyBtnText');
        if (preview) {
          preview.textContent = txt;
          preview.classList.add('shown');
        }
        try {
          await navigator.clipboard.writeText(txt);
          btn.classList.add('copied');
          if (btnLabel) btnLabel.textContent = 'Copied to Clipboard ✓';
          if (status) {
            status.textContent = 'Paste into Canvas → Submit Assignment → Text Entry → Submit.';
            status.className = 'copy-status success';
          }
          setTimeout(() => {
            btn.classList.remove('copied');
            if (btnLabel) btnLabel.textContent = 'Copy My Answers';
          }, 4000);
        } catch (e) {
          if (status) {
            status.textContent = 'Auto-copy blocked. Select all the text below (Ctrl+A / Cmd+A) and copy manually.';
            status.className = 'copy-status warn';
          }
          if (preview) {
            const range = document.createRange();
            range.selectNodeContents(preview);
            const sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
          }
        }
      });
    }
  };

  /* ===========================================================
     BOARD ENGINE — slide deck for the classroom-board view
     =========================================================== */
  Stratosyn.Board = {
    _slides: [],
    _idx: 0,

    init() {
      this._slides = $$('.slide');
      if (!this._slides.length) return;
      this._buildSceneStrip();
      this._wireKeys();
      this._wireButtons();
      this.go(0);
    },

    _buildSceneStrip() {
      const strip = $('#sceneStrip');
      if (!strip) return;
      strip.innerHTML = '';
      this._slides.forEach((s, i) => {
        const b = document.createElement('button');
        b.className = 'dot-btn';
        b.title = 'Slide ' + (i + 1);
        b.addEventListener('click', () => this.go(i));
        strip.appendChild(b);
      });
    },

    _wireKeys() {
      document.addEventListener('keydown', (e) => {
        if (e.target.matches('input, textarea')) return;
        if (e.key === 'ArrowRight' || e.key === ' ' || e.key === 'PageDown') {
          e.preventDefault();
          this.next();
        } else if (e.key === 'ArrowLeft' || e.key === 'PageUp') {
          e.preventDefault();
          this.prev();
        } else if (e.key === 'Home') {
          e.preventDefault();
          this.go(0);
        } else if (e.key === 'End') {
          e.preventDefault();
          this.go(this._slides.length - 1);
        }
      });
    },

    _wireButtons() {
      const btnPrev = $('#btnPrev');
      const btnNext = $('#btnNext');
      if (btnPrev) btnPrev.addEventListener('click', () => this.prev());
      if (btnNext) btnNext.addEventListener('click', () => this.next());
    },

    go(i) {
      i = Math.max(0, Math.min(this._slides.length - 1, i));
      this._idx = i;
      this._slides.forEach((s, n) => s.classList.toggle('active', n === i));
      const dots = $$('.dot-btn');
      dots.forEach((d, n) => d.classList.toggle('active', n === i));
      const ctr = $('#slideCtr');
      if (ctr) ctr.textContent = (i + 1) + ' / ' + this._slides.length;
      const pf = $('#hudFill');
      if (pf) pf.style.width = ((i + 1) / this._slides.length * 100) + '%';
      const btnPrev = $('#btnPrev');
      const btnNext = $('#btnNext');
      if (btnPrev) btnPrev.disabled = (i === 0);
      if (btnNext) btnNext.disabled = (i === this._slides.length - 1);
      // Logo fills based on slide progress
      Stratosyn.Logo.setFromRatio((i + 1) / this._slides.length);
    },

    next() { this.go(this._idx + 1); },
    prev() { this.go(this._idx - 1); }
  };

})(window);
