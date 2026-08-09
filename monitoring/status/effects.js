// servingz — shared sci-fi text effects (scramble-on-hover, decode-in,
// glow shimmer). Ported from the text treatment on zaindroid.me — no WebGL
// background here, text only.

const SCRAMBLE_CHARS = '!<>-_/[]{}—=+*^?#ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
const DECODE_CHARS = '!<>-_/[]{}#01▓░';
const REDUCE_MOTION = matchMedia('(prefers-reduced-motion: reduce)').matches;

function scrambleText(el, finalText, dur = 450) {
  if (REDUCE_MOTION) { el.textContent = finalText; return; }
  const t0 = performance.now();
  (function tick(now) {
    const t = Math.min(1, (now - t0) / dur);
    let out = '';
    for (let i = 0; i < finalText.length; i++) {
      out += (i / finalText.length < t)
        ? finalText[i]
        : SCRAMBLE_CHARS[Math.floor(Math.random() * SCRAMBLE_CHARS.length)];
    }
    el.textContent = out;
    if (t < 1) requestAnimationFrame(tick);
  })(t0);
}

// Wires hover-to-scramble on any [data-scramble] element under root. Safe
// to call repeatedly (e.g. after re-rendering a list) — already-wired
// elements are skipped.
function wireScrambleHovers(root = document) {
  root.querySelectorAll('[data-scramble]').forEach(el => {
    if (el.dataset.scrambleWired) return;
    el.dataset.scrambleWired = '1';
    const original = el.textContent;
    el.addEventListener('mouseenter', () => scrambleText(el, original, 420));
  });
}

// Decode-in: wraps text in per-letter spans, each resolving from noise to
// its real character on a staggered delay. Replaces el's content.
function decodeIn(el, text, { stagger = 26, dur = 240 } = {}) {
  if (REDUCE_MOTION) { el.textContent = text; return; }
  el.textContent = '';
  const spans = [...text].map(ch => {
    const s = document.createElement('span');
    s.className = 'dchar';
    s.textContent = ch === ' ' ? ' ' : ch;
    el.appendChild(s);
    return s;
  });
  spans.forEach((s, i) => {
    if (text[i] === ' ') return;
    const delay = i * stagger;
    const start = performance.now() + delay, end = start + dur;
    (function tick(n) {
      if (n < start) { requestAnimationFrame(tick); return; }
      if (n >= end) { s.textContent = text[i]; return; }
      s.textContent = DECODE_CHARS[Math.floor(Math.random() * DECODE_CHARS.length)];
      requestAnimationFrame(tick);
    })(performance.now());
  });
}

// Traveling glow pulse across an already decode-in'd element's letters.
function shimmer(el, everyMs = 6000) {
  if (REDUCE_MOTION) return;
  const wave = () => {
    el.querySelectorAll('.dchar').forEach((s, i) => {
      setTimeout(() => {
        s.classList.add('pulse');
        setTimeout(() => s.classList.remove('pulse'), 500);
      }, i * 55);
    });
  };
  setTimeout(() => { wave(); setInterval(wave, everyMs); }, 1200);
}

window.zorcEffects = { scrambleText, wireScrambleHovers, decodeIn, shimmer };
