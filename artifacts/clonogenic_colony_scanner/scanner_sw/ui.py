"""Front-panel UI for the scanner: a 2-inch 320x240 ST7789 SPI display and the RGB ring of the
19 mm pushbutton. One state machine, one place that decides what the user should be doing.

Wiring (Pi 5, BCM numbers; SPI0), through the two JST-XH sockets on the Perma-Proto HAT:
  J2 display (8)  3V3 · GND · MOSI GPIO10 · SCLK GPIO11 · CS GPIO8 (CE0) · DC GPIO25 · RST GPIO24 · BL GPIO18 (PWM)
  J1 button  (5)  switch: C1 -> GPIO17, NO1 -> GND (internal pull-up). Ring (PM192, common-ANODE "C+", 6 V version with
                  built-in resistors): C+ -> 5 V; Red/Green/Blue cathodes -> ULN2003 outputs on the HAT; ULN2003 inputs <- GPIO22/27/23.
                  The ULN inverts nothing from the GPIO's point of view: GPIO high = colour on, so RING_ACTIVE_LOW stays False.
Deps on the Pi:  pip install luma.lcd pillow   (luma >= 2.4 uses lgpio on the Pi 5 automatically)
"""
from __future__ import annotations
import threading, time
from PIL import Image, ImageDraw, ImageFont

RING_ACTIVE_LOW = False
COLORS = {"ready": (0, 255, 90), "busy": (255, 255, 255), "flip": (255, 140, 0), "error": (255, 0, 0), "off": (0, 0, 0), "focus": (40, 110, 255)}
STATES = {
    "ready":      ("READY",            "Lid UP · A1 rear-left\nslide in, close the flap"),
    "labels":     ("PHOTOGRAPHING",    "the lid — hold still"),
    "flip":       ("FLIP THE PLATE",   "toward you, lid DOWN\nslide in, close the flap"),
    "colonies":   ("PHOTOGRAPHING",    "the colonies"),
    "counting":   ("COUNTING",         ""),
    "done":       ("DONE",             ""),
    "error":      ("PROBLEM",          ""),
    "recovered":  ("RECOVERED",        ""),
    "focus":      ("FOCUS",            ""),
    "flash":      ("",                 ""),
}
RING = {"ready": "ready", "labels": "busy", "colonies": "busy", "counting": "busy", "flip": "flip", "done": "done", "error": "error", "recovered": "error", "flash": "busy", "focus": "focus"}
COLORS["done"] = (170, 90, 255)

class Panel:
    def __init__(self, simulate: bool = False):
        self.sim = simulate
        self.state, self.detail, self.counts, self.thumb = "ready", "", None, None
        self._blink = False; self._stop = False
        if not simulate:
            from luma.core.interface.serial import spi
            from luma.lcd.device import st7789
            from gpiozero import RGBLED, PWMLED
            self.dev = st7789(spi(port=0, device=0, gpio_DC=25, gpio_RST=24), width=320, height=240, rotate=0)
            self.bl = PWMLED(18); self.bl.value = 0.8
            self.ring = RGBLED(22, 27, 23, active_high=not RING_ACTIVE_LOW)
        self.font_big = self._font(30); self.font = self._font(20); self.font_small = self._font(15)
        threading.Thread(target=self._loop, daemon=True).start()

    def _font(self, size):
        for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
            try: return ImageFont.truetype(f, size)
            except OSError: pass
        return ImageFont.load_default()

    # ---- public API used by capture.py
    def set(self, state: str, detail: str = "", counts: dict | None = None, thumb: Image.Image | None = None, press: str = "", hold: str = "",
            focus: dict | None = None):
        self.state, self.detail, self.counts, self.thumb, self.press, self.hold = state, detail, counts, thumb, press, hold
        self.focus = focus
        self.t_state = time.time(); self.hold_frac = 0.0

    def focus_update(self, score: float, best: float, corners: float):
        """Live sharpness while the focus ring is being turned (called several times a second)."""
        self.focus = {"score": score, "best": best, "corners": corners}

    def flash(self, text: str, seconds: float = 2.0):
        self.set("flash", detail=text); time.sleep(seconds)

    def progress(self, frac: float):
        self.hold_frac = frac

    # ---- rendering
    def frame(self) -> Image.Image:
        im = Image.new("RGB", (320, 240), (12, 10, 16)); d = ImageDraw.Draw(im)
        title, body = STATES.get(self.state, (self.state.upper(), ""))
        col = COLORS[RING.get(self.state, "ready")]
        d.rectangle((0, 0, 320, 8), fill=col)
        if self.state == "flash":
            d.text((14, 100), self.detail, font=self.font, fill=(240, 236, 245)); return im
        d.text((14, 20), title, font=self.font_big, fill=(240, 236, 245))
        if self.state == "counting":
            d.text((200, 26), f"{int(time.time() - self.t_state)} s", font=self.font, fill=(160, 150, 175))
        y = 64
        for line in (self.detail or body).split("\n"):
            d.text((14, y), line, font=self.font, fill=(200, 195, 210)); y += 26
        # footer: what the button does right now, and the hold bar while holding
        d.rectangle((0, 206, 320, 240), fill=(24, 20, 32))
        if getattr(self, "press", ""): d.text((10, 210), "PRESS", font=self.font_small, fill=col); d.text((72, 210), self.press, font=self.font_small, fill=(230, 226, 238))
        if getattr(self, "hold", ""): d.text((10, 225), "HOLD", font=self.font_small, fill=(255, 140, 0)); d.text((72, 225), self.hold, font=self.font_small, fill=(230, 226, 238))
        if getattr(self, "hold_frac", 0) > 0: d.rectangle((0, 202, int(320 * self.hold_frac), 206), fill=(255, 140, 0))
        if self.state == "focus":
            f = self.focus or {"score": 0.0, "best": 1e-9, "corners": 0.0}
            frac = min(1.0, f["score"] / max(f["best"], 1e-9)); cfrac = min(1.0, f["corners"] / max(f["best"], 1e-9))
            d.text((14, 58), "turn the focus ring to the peak,", font=self.font_small, fill=(200, 195, 210))
            d.text((14, 76), "then lock its screw", font=self.font_small, fill=(200, 195, 210))
            d.text((14, 100), f"{100 * frac:3.0f}", font=self.font_big, fill=(240, 236, 245))
            d.text((80, 112), "of best so far", font=self.font_small, fill=(160, 150, 175))
            d.rectangle((14, 140, 306, 162), outline=(90, 80, 110))                      # centre sharpness bar
            d.rectangle((16, 142, 16 + int(288 * frac), 160), fill=COLORS["focus"] if frac < 0.97 else COLORS["ready"])
            d.line((16 + int(288 * 1.0), 136, 16 + int(288 * 1.0), 166), fill=(240, 236, 245), width=2)   # peak marker
            d.text((14, 170), "corners", font=self.font_small, fill=(160, 150, 175))
            d.rectangle((92, 172, 306, 184), outline=(90, 80, 110))
            d.rectangle((94, 174, 94 + int(210 * cfrac), 182), fill=(150, 140, 170))
            return im
        if self.state == "done" and self.counts:
            names = ["A1", "A2", "A3", "B1", "B2", "B3"]
            for i, w in enumerate(names):
                x, yy = 14 + (i % 3) * 66, 62 + (i // 3) * 52
                d.rectangle((x, yy, x + 58, yy + 44), outline=(90, 80, 110))
                d.text((x + 6, yy + 3), w, font=self.font_small, fill=(160, 150, 175))
                d.text((x + 6, yy + 17), str(self.counts.get(w, "–")), font=self.font, fill=(240, 236, 245))
            if self.thumb is not None:
                t = self.thumb.copy(); t.thumbnail((100, 100)); im.paste(t, (214, 62))
        return im

    def _loop(self):
        while not self._stop:
            self._blink = not self._blink
            if not self.sim:
                self.dev.display(self.frame())
                col = COLORS[RING.get(self.state, "ready")]
                if self.state == "flip" and not self._blink: col = COLORS["off"]
                if self.state == "done": col = tuple(int(c * (0.35 + 0.65 * self._blink)) for c in COLORS["done"])
                if self.state == "focus" and self.focus:                       # ring brightness follows sharpness; green at the peak
                    frac = min(1.0, self.focus["score"] / max(self.focus["best"], 1e-9))
                    col = COLORS["ready"] if frac >= 0.97 else tuple(int(c * (0.12 + 0.88 * frac)) for c in COLORS["focus"])
                self.ring.color = tuple(c / 255 for c in col)
            time.sleep(0.5 if self.state != "focus" else 0.1)

if __name__ == "__main__":       # render every state to PNGs for a look without hardware
    p = Panel(simulate=True)
    hints = {"ready": ("photograph the lid", "focus mode"), "focus": ("done", "reset the peak"), "flip": ("photograph the colonies", "discard this run"), "counting": ("skip counting", "discard this run"),
             "done": ("next plate", "reject this run"), "error": ("retry", "dismiss"), "recovered": ("continue", "")}
    for st in STATES:
        pr, ho = hints.get(st, ("", ""))
        p.set(st, detail="camera not found" if st == "error" else ("1 unfinished run moved\nto _incomplete" if st == "recovered" else ("run discarded" if st == "flash" else "")),
              counts={"A1": 12, "A2": 6, "A3": 17, "B1": 23, "B2": 21, "B3": 6} if st == "done" else None, press=pr, hold=ho)
        if st == "flip": p.hold_frac = 0.6
        if st == "focus": p.focus = {"score": 0.71, "best": 1.0, "corners": 0.55}
        p.frame().save(f"/tmp/panel_{st}.png")
    print("wrote /tmp/panel_*.png")
