# Operating the scanner — one button, one screen, no ambiguity

**Where to stand (rev 3).** The screen and button are on the electronics cassette at the back of the
box, so set the box with that face toward you; the loading slot is then on your **left**. You slide
plates in and out with your left hand and never look away from the screen. Everything below is
written for that position: "A1 rear-left" and "flip toward you" are as seen from the slot side, so
from your seat that is A1 at the far-left corner and a flip about the axis that runs left–right.

The front has a 2-inch screen and a 16 mm steel button. The screen always
shows three things: **what state the scanner is in**, **what to do**, and a footer with **what a
PRESS does and what a HOLD does right now**. The colour bar across the top of the screen shows the state so you can see it
from across the bench.

| Screen bar | State | Screen says | Press (tap) | Hold (2 s) |
|---|---|---|---|---|
| green, steady | READY | Lid UP · A1 rear-left · slide in, close the flap | photograph the lid | focus mode |
| blue, brightness follows sharpness | FOCUS | sharpness bar with a peak marker | done | reset the peak |
| white | PHOTOGRAPHING the lid | hold still (about 1 s) | ignored | ignored |
| **amber, blinking** | FLIP THE PLATE | toward you, lid DOWN · slide in, close the flap | photograph the colonies | discard this run |
| white | PHOTOGRAPHING the colonies | | ignored | ignored |
| white | COUNTING (elapsed seconds) | | skip counting, keep the raw frames | discard this run |
| **purple, pulsing** | DONE — six counts + thumbnail | | next plate | reject this run |
| **red** | PROBLEM — what went wrong | | retry | dismiss |

While you hold the button an orange bar fills across the screen; let go before it fills and
nothing happens. That is the whole language: tap = go, hold = no.

## Where you stand (rev 4.0)

The flap is at the bottom of the front, the screen above it, the button on the roof beside the camera. Pressing the
button pushes straight down into the bench, so it cannot rock the box. Lift the flap past vertical and it parks
against the tower, so both hands are free for the plate. "A1 rear-left" means the far-left corner from where you
stand; "toward you" means the flip brings the lid down on the near side.

## Focus mode

Hold the button at READY and the screen becomes a sharpness meter: a bar with a peak marker, the score as a
percentage of the best value seen, and a second bar for the corners of the field. The bar's brightness
follows the score and turns green within 3% of the peak, so you can watch it from under the tower while
your hand is on the lens. Tap to leave; hold again to reset the peak. Lift the tower onto blocks with a
stained plate flipped on the pad, set the aperture to F5.6, turn the focus ring until the bar peaks, lock
the screw, put the tower back. The same meter prints in a terminal with `capture.py --focus`.

## The cases you asked about

- **Abort a run.** Hold the button at any waiting point (FLIP, COUNTING, DONE). At FLIP or COUNTING
  the run is deleted; at DONE it is kept but moved to `_rejected/` so it never mixes with good data.
- **Walked away mid-run.** FLIP waits five minutes, then files the label photo under `_incomplete/`
  and returns to READY. DONE returns to READY after a minute (the data are already saved).
- **Not sure what a press will do.** Read the footer. It changes with the state, and the bar
  colour tells you which state that is before you touch anything.
- **Power cut or crash mid-run.** Every step is written to `run_status.json` in the run's folder
  the moment it starts, so a folder never claims more than actually happened. At boot the scanner
  finds any run still marked in progress, moves it to `_incomplete/`, and tells you on screen.
- **Restart the software.** Unplug and replug the Pi, or `sudo systemctl restart scanner` from the
  Mac. Nothing is lost: raw frames are written before counting begins.
- **Camera or counting failure.** Red bar, the message on screen, and the run's raw frames are
  kept in `_incomplete/` if they exist. Tap to retry the failed step, hold to dismiss.
- **Toss a plate after seeing the counts.** Hold at DONE: it goes to `_rejected/`, still on disk,
  still with its label photo, out of the good-data folder.

## Where the data go

`scans/<timestamp>/` for good runs (`lid_labels.png`, `bottom_raw_stack.npy`, `od.npy`, `overlay.png`,
`colonies.csv`, `wells.csv`, `summary.json`, `run_status.json`); `scans/_incomplete/` and
`scans/_rejected/` for the rest. The Mac's watcher only pulls from `scans/` proper. Runs are
named by time; the label photo is the plate's identity.
