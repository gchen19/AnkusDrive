#!/usr/bin/env python3
"""Pi-side capture: one button press -> LEDs on -> N raw frames (bottom HQ camera) + one JPEG
(top Camera Module 3) -> LEDs off -> scan folder + metadata -> quantify.py.

  python3 capture.py                 # wait for the button (systemd service)
  python3 capture.py --once NAME     # one scan now, folder named NAME
  python3 capture.py --dark / --flat # calibration frames into paths.calib_dir
  python3 capture.py --focus         # print the sharpness meter to the terminal (the panel has the same meter: hold at READY)

Raw frames are saved UNFILTERED as 16-bit .npy (linear, 12-bit range); everything downstream
is derived and regenerable from them.
"""
from __future__ import annotations
import argparse, json, shutil, subprocess, time
from pathlib import Path
import numpy as np, yaml
from runlog import run_log
try:
    from ui import Panel
except Exception:            # no display libs on this machine
    Panel = None

HERE = Path(__file__).resolve().parent
CFG = yaml.safe_load(open(HERE / "config.yaml"))

def _gpio():
    from gpiozero import LED, Button
    g = CFG["gpio"]
    return LED(g["led_pin"]), Button(g["button_pin"], pull_up=True, bounce_time=0.05), LED(g["status_led_pin"])

def open_cameras():
    from picamera2 import Picamera2
    cams = Picamera2.global_camera_info()
    idx_hq = next(i for i, c in enumerate(cams) if "imx477" in c["Model"].lower())
    idx_top = next((i for i, c in enumerate(cams) if "imx708" in c["Model"].lower()), None)
    c = CFG["camera"]
    hq = Picamera2(idx_hq)
    hq.configure(hq.create_still_configuration(raw={"format": c["raw_format"], "size": tuple(c["raw_size"])}))
    hq.set_controls({"ExposureTime": int(c["exposure_us"]), "AnalogueGain": float(c["analogue_gain"]),
                     "AeEnable": False, "AwbEnable": False})
    top = None
    if idx_top is not None:                                            # variant A only
        top = Picamera2(idx_top)
        top.configure(top.create_still_configuration(main={"size": (4608, 2592)}))
        top.set_controls({"ExposureTime": int(c["top_exposure_us"]), "AnalogueGain": 1.0, "AeEnable": False,
                          "AfMode": 2})
        top.start()
    hq.start(); time.sleep(0.5)
    return hq, top

class fan_paused:
    """Stop the Pi 5 Active Cooler for the moment the shutter is open (its 30 mm rotor shares the ceiling with the
    camera). Firmware cooling device 0: state 0 = fan off; restored on exit. Silently a no-op where not writable."""
    PATH = "/sys/class/thermal/cooling_device0/cur_state"
    def __enter__(self):
        try:
            self.prev = open(self.PATH).read().strip(); open(self.PATH, "w").write("0"); time.sleep(0.15)
        except Exception: self.prev = None
        return self
    def __exit__(self, *a):
        if self.prev is not None:
            try: open(self.PATH, "w").write(self.prev)
            except Exception: pass

def grab_raw_stack(hq, n):
    frames = []
    with fan_paused():
      for _ in range(n):
        arr = hq.capture_array("raw")                 # uint16 for the unpacked format
        if arr.dtype == np.uint8:                     # packed fallback -> view as u16 (unpacked formats avoid this)
            arr = arr.view(np.uint16)
        frames.append(arr.astype(np.uint16))
    return np.stack(frames)

def sharpness(g: np.ndarray) -> float:
    """Exposure-independent focus measure: variance of the 4-neighbour Laplacian over the local mean squared."""
    g = g.astype(np.float32)
    lap = 4 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1] - g[1:-1, :-2] - g[1:-1, 2:]
    return float(lap.var() / max(g.mean() ** 2, 1e-6))

def focus_score(hq, patch=400):
    """One raw frame -> (centre score, mean corner score) on the green sites. Corners are the four
    points at 40% of the half-field, so a tilted camera plate shows up as corners lagging the centre."""
    raw = hq.capture_array("raw")
    if raw.dtype == np.uint8: raw = raw.view(np.uint16)
    g = raw[0::2, 1::2]                                             # green sites, half resolution
    H, W = g.shape; h = patch // 2
    def at(cy, cx): return sharpness(g[cy - h:cy + h, cx - h:cx + h])
    centre = at(H // 2, W // 2)
    corners = np.mean([at(int(H * fy), int(W * fx)) for fy in (0.3, 0.7) for fx in (0.3, 0.7)])
    return centre, float(corners)

def scan_flip(hq, leds, status, button, name: str, panel=None):
    """Variant B: one camera. Press 1 = plate lid-up (labels). Status LED then blinks until the
    plate is flipped and the button pressed again (colonies). Same focus for both."""
    out = Path(CFG["paths"]["scans_dir"]) / name; out.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    with run_log("capture_scan", CFG["paths"]["runs_dir"], config=CFG["camera"]) as rec:
        if panel: panel.set("labels")
        leds.on(); time.sleep(0.3)
        lab = grab_raw_stack(hq, 1)[0]
        g = lab[0::2, 1::2].astype(np.float32); g = (255 * np.clip(g / max(1.0, np.percentile(g, 99.5)), 0, 1)).astype(np.uint8)
        Image.fromarray(g).save(out / "lid_labels.png")             # green sites, half-res, plenty for reading
        if panel: panel.set("flip")
        status.blink(0.15, 0.15)                                       # "flip it"
        button.wait_for_release(); button.wait_for_press(); status.on()
        if panel: panel.set("colonies")
        stack = grab_raw_stack(hq, CFG["camera"]["frames_to_average"])
        leds.off()
        np.save(out / "bottom_raw_stack.npy", stack)
        meta = {"name": name, "kind": "scan", "variant": "v2_flip", "t": time.time(), "camera": CFG["camera"],
                "bottom_metadata": hq.capture_metadata(), "frames": int(stack.shape[0])}
        (out / "meta.json").write_text(json.dumps(meta, indent=1, default=str))
        rec["outputs"] += [str(out / "lid_labels.png"), str(out / "bottom_raw_stack.npy"), str(out / "meta.json")]
    if panel: panel.set("quantify")
    subprocess.run(["python3", str(HERE / "quantify.py"), str(out)], check=False)
    if CFG["paths"]["nas_sync"]:
        subprocess.run(["rsync", "-a", str(out), CFG["paths"]["nas_sync"]], check=False)
    if panel:
        try:
            sm = json.loads((out / "summary.json").read_text()); counts = {w["well"]: w["n_colonies"] for w in sm["wells"]}
            from PIL import Image as _I; thumb = _I.open(out / "overlay.png")
            panel.set("done", counts=counts, thumb=thumb)
        except Exception as e:
            panel.set("error", detail=str(e)[:40])
    return out

def scan(hq, top, leds, name: str, kind: str = "scan"):
    out = Path(CFG["paths"]["scans_dir" if kind == "scan" else "calib_dir"]) / name
    out.mkdir(parents=True, exist_ok=True)
    with run_log(f"capture_{kind}", CFG["paths"]["runs_dir"], config=CFG["camera"]) as rec:
        if kind != "dark":
            leds.on(); time.sleep(0.3)                # LED + sensor settle
        stack = grab_raw_stack(hq, CFG["camera"]["frames_to_average"])
        top_img = None
        if kind == "scan":
            top_img = top.capture_array("main")
        leds.off()
        np.save(out / "bottom_raw_stack.npy", stack)   # unfiltered, all frames
        if top_img is not None:
            from PIL import Image
            Image.fromarray(top_img).save(out / "top_labels.jpg", quality=92)
        meta = {"name": name, "kind": kind, "t": time.time(), "camera": CFG["camera"],
                "bottom_metadata": hq.capture_metadata(), "frames": int(stack.shape[0])}
        (out / "meta.json").write_text(json.dumps(meta, indent=1, default=str))
        rec["outputs"] += [str(out / "bottom_raw_stack.npy"), str(out / "meta.json")]
    if kind == "scan":
        subprocess.run(["python3", str(HERE / "quantify.py"), str(out)], check=False)
        if CFG["paths"]["nas_sync"]:
            subprocess.run(["rsync", "-a", str(out), CFG["paths"]["nas_sync"]], check=False)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", metavar="NAME")
    ap.add_argument("--dark", action="store_true"); ap.add_argument("--flat", action="store_true")
    ap.add_argument("--focus", action="store_true", help="live sharpness meter in the terminal; Ctrl-C to stop")
    a = ap.parse_args()
    leds, button, status = _gpio()
    hq, top = open_cameras()
    if a.focus:
        leds.on(); best = 1e-9
        try:
            while True:
                c, k = focus_score(hq); best = max(best, c)
                print(f"\rcentre {100 * c / best:5.1f}% of best   corners {100 * k / best:5.1f}%   {'<< PEAK' if c / best > 0.97 else ''}      ", end="", flush=True)
        except KeyboardInterrupt:
            leds.off(); print(); return
    if a.dark:  return scan(hq, top, leds, "dark", "dark")
    if a.flat:  return scan(hq, top, leds, "flat", "flat")
    if a.once:  return scan(hq, top, leds, a.once)
    flip = CFG.get("instrument", {}).get("variant") == "v2_flip"
    if not flip:
        print("ready — press the button")
        while True:
            button.wait_for_press(); status.on(); scan(hq, top, leds, time.strftime("%Y%m%d_%H%M%S")); status.off()
    # variant B: one button, one screen, one state machine (runflow.py)
    from runflow import Controller, Button as RFButton
    from PIL import Image as _I
    panel = Panel() if (Panel and CFG.get("instrument", {}).get("panel", True)) else None
    class _NoPanel:
        def set(self, *a, **k): print("PANEL", a, {k_: v for k_, v in k.items() if k_ in ("press", "hold", "detail")})
        def flash(self, t, s=1.0): print("PANEL flash", t); time.sleep(s)
        def progress(self, f): pass
    panel = panel or _NoPanel()
    rf_button = RFButton(CFG["gpio"]["button_pin"], on_progress=panel.progress)
    def cap_labels(folder):
        leds.on(); time.sleep(0.3); lab = grab_raw_stack(hq, 1)[0]
        g = lab[0::2, 1::2].astype(np.float32); g = (255 * np.clip(g / max(1.0, np.percentile(g, 99.5)), 0, 1)).astype(np.uint8)
        _I.fromarray(g).save(folder / "lid_labels.png")
    def cap_colonies(folder):
        stack = grab_raw_stack(hq, CFG["camera"]["frames_to_average"]); leds.off()
        np.save(folder / "bottom_raw_stack.npy", stack)
        (folder / "meta.json").write_text(json.dumps({"name": folder.name, "kind": "scan", "variant": "v2_flip", "t": time.time(),
                                                      "camera": CFG["camera"], "bottom_metadata": hq.capture_metadata(), "frames": int(stack.shape[0])}, indent=1, default=str))
    def quant(folder):
        subprocess.run(["python3", str(HERE / "quantify.py"), str(folder)], check=True)
        if CFG["paths"]["nas_sync"]: subprocess.run(["rsync", "-a", str(folder), CFG["paths"]["nas_sync"]], check=False)
        sm = json.loads((folder / "summary.json").read_text())
        return {w["well"]: w["n_colonies"] for w in sm["wells"]}, _I.open(folder / "overlay.png")
    def focus_meter():
        leds.on(); return focus_score(hq)
    Controller(panel, rf_button, cap_labels, cap_colonies, quant, CFG["paths"]["scans_dir"], CFG["paths"]["runs_dir"], focus_score=focus_meter).loop()

if __name__ == "__main__":
    main()
