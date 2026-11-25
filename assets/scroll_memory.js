// Per-tab scroll memory for OneChain.
//
// The dashboard scrolls at the window level, and Dash replaces #tab-content (and
// toggles the persistent Advanced / Options-Analysis containers) on every main-tab
// and sub-tab switch — so the browser forgets where you were scrolled and snaps to
// the top. This module remembers window.scrollY per (main tab + active pair sub-tab)
// and restores it, with sensible first-visit defaults (Chain ~centered, others top).
//
// It is pure client-side: by the time a Dash callback fires the old DOM (and its
// scroll position) is already gone, so this has to live in the browser. The small
// clientside callback in ui/shell.py calls window.ocScrollRestore(key) once the new
// active (tab, sub-tab) is known and the content has re-rendered.

(function () {
  "use strict";

  var STORAGE_KEY = "onechain.scrollMemory";
  var positions = {};
  try {
    positions = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || "{}") || {};
  } catch (e) {
    positions = {};
  }

  var currentKey = null;
  var suspended = false;        // ignore scroll-capture during/just-after a restore
  var suspendWatchdog = null;   // safety so we never get stuck suspended
  var rafThrottle = null;

  function persist() {
    try {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(positions));
    } catch (e) {}
  }

  function scrollY() {
    return window.scrollY || window.pageYOffset || 0;
  }

  function saveCurrent() {
    if (suspended || currentKey === null) return;
    positions[currentKey] = scrollY();
    persist();
  }

  function suspendCapture() {
    suspended = true;
    if (suspendWatchdog) clearTimeout(suspendWatchdog);
    // If a tab is re-clicked with no re-render, the restore may never run — make sure
    // capture resumes regardless.
    suspendWatchdog = setTimeout(function () { suspended = false; }, 700);
  }

  function maxScroll() {
    return Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
  }

  function defaultFor(key) {
    // Chain ~centered on first visit; all other tabs at the top.
    if (key && key.indexOf("chain-tab") === 0) {
      return Math.round(maxScroll() * 0.5);
    }
    return 0;
  }

  // Restore over several animation frames: the chain DataTable paints progressively
  // and grows the page, so the target Y may be unreachable for the first few frames.
  function restore(key) {
    var hasSaved = Object.prototype.hasOwnProperty.call(positions, key);
    var attempts = 0;
    function step() {
      var target = hasSaved ? positions[key] : defaultFor(key);
      var max = maxScroll();
      var clamped = Math.min(target, max);
      window.scrollTo(0, clamped);
      attempts += 1;
      var settled = max >= target && Math.abs(scrollY() - clamped) <= 2;
      if (attempts < 30 && !settled) {
        requestAnimationFrame(step);
      } else {
        suspended = false;
        if (suspendWatchdog) { clearTimeout(suspendWatchdog); suspendWatchdog = null; }
      }
    }
    suspendCapture();
    requestAnimationFrame(step);
  }

  // Capture: one save per animation frame while the user scrolls.
  window.addEventListener("scroll", function () {
    if (rafThrottle) return;
    rafThrottle = requestAnimationFrame(function () {
      rafThrottle = null;
      saveCurrent();
    });
  }, { passive: true });

  // Early-save the outgoing tab's position the instant a tab / sub-tab nav is clicked,
  // BEFORE Dash re-renders and the window snaps to the top.
  document.addEventListener("click", function (ev) {
    if (!ev.target || !ev.target.closest) return;
    var nav = ev.target.closest("#main-tabs .nav-link, #loaded-pair-tabs .nav-link");
    if (!nav) return;
    saveCurrent();
    suspendCapture();
  }, true);

  // Called by the Dash clientside callback once the active (main tab, sub tab) is known
  // and the new content has rendered.
  window.ocScrollRestore = function (key) {
    if (!key) return;
    currentKey = key;
    restore(key);
  };
})();
