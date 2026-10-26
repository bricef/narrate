// narrate studio: an HTML scene in the preview, on a clock the studio moves (#73, design D12).
//
// A plain script, run in the page before the page's own scripts: after the fake clock
// (vendor/fake-timers.js), the renderer's own seeking (window.__narrateSeek, pages._SEEK_JS) and the
// cues. The studio posts {narrate: "time", time} with the page's time in seconds, and the page shows
// that moment as a build captures it (pages.py): the clock moved on one frame at a time, running
// timers, requestAnimationFrame and Date that far, and every animation seeked after each step.
//
// Timers can't run backwards, so going back reloads a page that has scripts of its own (the studio
// posts the time again once it has loaded); a page without scripts seeks straight there.
(() => {
  const FPS = 30;
  const timers = ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "requestAnimationFrame",
                  "cancelAnimationFrame", "Date", "performance"];
  if ("requestIdleCallback" in window) timers.push("requestIdleCallback", "cancelIdleCallback");
  const clock = NarrateFakeTimers.withGlobal(window).install({
    now: 0,
    toFake: timers,
  });
  const seek = window.__narrateSeek;
  const ms = (frame) => Math.round((frame * 1000) / FPS);
  const scripted = () => Array.from(document.scripts).some((s) => !s.hasAttribute("data-narrate"));
  let frame = 0;
  let loaded = false;
  let wanted = null;

  function show(time) {
    const target = Math.max(0, Math.floor(time * FPS + 1e-6));
    if (!scripted()) {
      frame = target;
      seek(frame / FPS);
    } else if (target < frame) {
      loaded = false;
      location.reload();
    } else {
      while (frame < target) {
        frame += 1;
        clock.tick(ms(frame) - ms(frame - 1));
        seek(frame / FPS);
      }
    }
    document.documentElement.dataset.narrateFrame = frame;  // for tests: the frame on show
  }

  addEventListener("message", (e) => {
    if (e.source !== parent || !e.data || e.data.narrate !== "time") return;
    if (loaded) show(e.data.time);
    else wanted = e.data.time;
  });
  addEventListener("load", () => {
    seek(0);  // animations running from the start began with the page, not at the first moment shown
    loaded = true;
    document.documentElement.dataset.narrateFrame = 0;
    if (wanted !== null) show(wanted);
  });
})();
