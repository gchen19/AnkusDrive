"""The scanner's one-button interaction model, as a state machine you can read in a minute.

  SHORT press  = go / confirm / next plate
  LONG press   = cancel / discard / dismiss   (2 s; the screen shows a hold bar filling up)
  The screen footer ALWAYS says what PRESS and HOLD do right now; the colour bar across its top says the state.

  READY      green        press: photograph the lid                 hold: FOCUS mode
  FOCUS      blue         live sharpness meter while you turn the lens ring; the bar's brightness follows
                          the score and turns green at the best value seen. press: done   hold: reset the peak
  LABELS     white        (capturing, ~1 s; button ignored)
  FLIP       amber blink  press: photograph the colonies            hold: discard this run
             5 min with no press -> INCOMPLETE (labels kept in _incomplete/, then READY)
  COLONIES   white        (capturing 4 raw frames; button ignored)
  COUNTING   white        press: skip counting (raw kept)           hold: discard this run
  DONE       purple pulse press: next plate                          hold: reject this run (moves it to _rejected/)
             60 s -> READY
  ERROR      red          press: retry the step                      hold: dismiss to READY
Power loss or a crash mid-run: at boot, any run-record still 'running' is shown once ("recovered:
last run incomplete"), its folder moved to _incomplete/, and the machine goes READY.
Every transition is written to the run-record, so a folder never lies about how it ended.
"""
from __future__ import annotations
import json, shutil, subprocess, threading, time
from pathlib import Path

HOLD_S, FLIP_TIMEOUT_S, DONE_TIMEOUT_S = 2.0, 300.0, 60.0

class Button:
    """Wraps gpiozero.Button into short/long press events; `sim=True` reads keyboard lines instead."""
    def __init__(self, pin=17, sim=False, on_progress=None):
        self.short = threading.Event(); self.long = threading.Event(); self.on_progress = on_progress or (lambda f: None)
        if sim:
            threading.Thread(target=self._sim, daemon=True).start(); return
        from gpiozero import Button as GB
        self.b = GB(pin, pull_up=True, bounce_time=0.03, hold_time=HOLD_S)
        self._t0 = None
        self.b.when_pressed = self._down; self.b.when_released = self._up; self.b.when_held = self._held
    def _down(self): self._t0 = time.time(); threading.Thread(target=self._bar, daemon=True).start()
    def _bar(self):
        while self._t0 and self.b.is_pressed and time.time() - self._t0 < HOLD_S:
            self.on_progress((time.time() - self._t0) / HOLD_S); time.sleep(0.05)
        self.on_progress(0.0)
    def _held(self): self._t0 = None; self.long.set()
    def _up(self):
        if self._t0 is not None and time.time() - self._t0 < HOLD_S: self.short.set()
        self._t0 = None; self.on_progress(0.0)
    def _sim(self):
        import sys
        for line in sys.stdin:
            (self.long if line.strip().lower().startswith("h") else self.short).set()
    def wait(self, timeout=None):
        """Returns 'short', 'long' or None (timeout)."""
        t0 = time.time()
        while timeout is None or time.time() - t0 < timeout:
            if self.short.is_set(): self.short.clear(); return "short"
            if self.long.is_set(): self.long.clear(); return "long"
            time.sleep(0.02)
        return None
    def clear(self): self.short.clear(); self.long.clear()

class Controller:
    def __init__(self, panel, button, capture_labels, capture_colonies, quantify, scans_dir, runs_dir, log=print, focus_score=None):
        self.panel, self.button, self.log = panel, button, log
        self.focus_score = focus_score                     # callable -> (centre_score, corner_score), or None
        self.capture_labels, self.capture_colonies, self.quantify = capture_labels, capture_colonies, quantify
        self.scans, self.runs = Path(scans_dir), Path(runs_dir)
        for d in ("_incomplete", "_rejected"): (self.scans / d).mkdir(parents=True, exist_ok=True)

    # ---- run-record bookkeeping (append-only JSON next to the scan folder)
    def _mark(self, folder: Path, status: str, **kw):
        rec = folder / "run_status.json"
        d = json.loads(rec.read_text()) if rec.exists() else {"events": []}
        d["status"] = status; d["events"].append({"t": time.time(), "status": status, **kw}); rec.write_text(json.dumps(d, indent=1))

    def _move(self, folder: Path, bucket: str):
        dst = self.scans / bucket / folder.name
        shutil.move(str(folder), str(dst)); return dst

    def recover(self):
        """Boot: any folder whose status is still 'running' becomes _incomplete."""
        n = 0
        for f in sorted(self.scans.glob("20*")):
            st = f / "run_status.json"
            if st.exists() and json.loads(st.read_text()).get("status") in ("labels", "flip", "colonies", "counting"):
                self._mark(f, "incomplete", reason="recovered at boot"); self._move(f, "_incomplete"); n += 1
        if n:
            self.panel.set("recovered", detail=f"{n} unfinished run{'s' if n > 1 else ''} moved\nto _incomplete", press="continue", hold="")
            self.button.wait(timeout=10)

    def focus_mode(self):
        """Hold at READY: stream sharpness to the screen and the ring until a tap. Hold again resets the peak."""
        best = 1e-9
        self.panel.set("focus", press="done", hold="reset the peak")
        self.log("focus mode")
        while True:
            try: centre, corners = self.focus_score()
            except Exception as e:
                self.panel.set("error", detail=f"focus meter failed\n{str(e)[:38]}", press="", hold="dismiss"); self.button.wait(); return
            best = max(best, centre)
            self.panel.focus_update(centre, best, corners)
            ev = self.button.wait(timeout=0.05)
            if ev == "short": return
            if ev == "long": best = 1e-9

    def run_one(self):
        self.button.clear()
        self.panel.set("ready", press="photograph the lid", hold="focus mode" if self.focus_score else "")
        ev = self.button.wait()
        if ev == "long" and self.focus_score: return self.focus_mode()
        if ev != "short": return
        name = time.strftime("%Y%m%d_%H%M%S"); folder = self.scans / name; folder.mkdir(parents=True, exist_ok=True)
        self._mark(folder, "labels")
        self.panel.set("labels", press="", hold="")
        try: self.capture_labels(folder)
        except Exception as e: return self._error(folder, "label photo failed", e)
        self._mark(folder, "flip")
        self.panel.set("flip", press="photograph the colonies", hold="discard this run")
        ev = self.button.wait(timeout=FLIP_TIMEOUT_S)
        if ev == "long":
            self._mark(folder, "discarded", reason="user cancelled at flip"); shutil.rmtree(folder, ignore_errors=True)
            return self.panel.flash("run discarded")
        if ev is None:
            self._mark(folder, "incomplete", reason="no flip within 5 min"); self._move(folder, "_incomplete")
            return self.panel.flash("timed out, kept as incomplete")
        self._mark(folder, "colonies")
        self.panel.set("colonies", press="", hold="")
        try: self.capture_colonies(folder)
        except Exception as e: return self._error(folder, "colony capture failed", e)
        self._mark(folder, "counting")
        self.panel.set("counting", press="skip counting", hold="discard this run")
        done = threading.Event(); result = {}
        def work():
            try: result["counts"], result["thumb"] = self.quantify(folder)
            except Exception as e: result["err"] = e
            done.set()
        threading.Thread(target=work, daemon=True).start()
        while not done.is_set():
            ev = self.button.wait(timeout=0.2)
            if ev == "long":
                self._mark(folder, "discarded", reason="user cancelled during counting"); shutil.rmtree(folder, ignore_errors=True)
                return self.panel.flash("run discarded")
            if ev == "short":
                self._mark(folder, "captured", reason="counting skipped by user"); return self.panel.flash("kept without counts")
        if "err" in result:
            self._mark(folder, "captured", reason=f"counting failed: {result['err']}")
            return self._error(folder, "counting failed, raw kept", result["err"], dismiss_only=True)
        self._mark(folder, "done")
        self.panel.set("done", counts=result["counts"], thumb=result["thumb"], press="next plate", hold="reject this run")
        ev = self.button.wait(timeout=DONE_TIMEOUT_S)
        if ev == "long":
            self._mark(folder, "rejected", reason="user rejected after seeing counts"); self._move(folder, "_rejected")
            self.panel.flash("run rejected")

    def _error(self, folder, msg, exc, dismiss_only=False):
        self.log(f"ERROR {msg}: {exc}")
        self._mark(folder, "error", reason=f"{msg}: {exc}")
        self.panel.set("error", detail=f"{msg}\n{str(exc)[:38]}", press="" if dismiss_only else "retry", hold="dismiss")
        ev = self.button.wait()
        if ev == "short" and not dismiss_only: return "retry"
        self._move(folder, "_incomplete")

    def loop(self):
        self.recover()
        while True:
            self.run_one()
