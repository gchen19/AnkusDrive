# Print quotes for frame.stl + tower.stl + door.stl — collected 2026-09-09 with Playwright

Uploaded the three variant-B STLs (frame 82.6 cm³, tower 314.9 cm³, door 18.5 cm³; tower is
176 × 106 × 170 mm) to each service's instant-quote tool, no account, no order placed. Screenshots
alongside this file. Prices in USD, single set, delivered to California.

## Summary — sorted by what I would actually do

| Rank | Service · material | Parts | Shipping | **Delivered** | Lead time | Notes |
|---|---|---|---|---|---|---|
| **1** | **Craftcloud · PETG, black, FDM, 20 % infill** — Corvallis3D, Oregon (4.88★, 283 reviews) | $57.30 | $19.56 UPS | **$76.86** | 7–10 d production + 2–5 d ship → arrives ~Sep 22–30 | cheapest black FDM; same vendor $86.11 with 5–7 d production, $94.48 with express ship (Sep 17–23) |
| 2 | Craftcloud · PLA, FDM | $57.88 | $14.90 | $72.78 | similar | $4 cheaper than PETG; PLA is fine on a bench but softens in a hot car |
| 3 | Craftcloud · ABS / ASA, FDM | $70.40 / $73.64 | $13.78 / $19.56 | $84 / $93 | similar | ASA only if you want UV-stable; not needed indoors |
| 4 | JLC3DP · PLA-P black FDM (frame + tower) + door in JLC Black Resin | $67.87 + $3.80 | ~$26 (1.4 kg, Global Standard Direct Line) | **≈ $98** | 3 d build + 8–13 business days ship | JLC's FDM minimum part size (30×30×10 mm) rejects the 3 mm door, hence resin for it |
| 5 | JLC3DP · SLA "JLC Black Resin", sanded, all three | $85.33 | $26.01 | $111.34 | 3 d + 8–13 d | dimensionally the best; black but gloss-ish, and 3 mm resin walls at 170 mm tall can warp |
| 6 | Craftcloud · Resin (cheapest resin offer) | $95.30 | $15.83 | $111.13 | | |
| 7 | JLC3DP · MJF PA12 nylon dyed black | $156.84 | $26.01 | $182.85 | | premium; overkill |
| 8 | JLC3DP · ASA black FDM (frame + tower) + resin door | $166.05 + $3.80 | ~$26 | ≈ $196 | | JLC prices ASA like an engineering material; skip |
| 9 | Craftcloud · SLS PA12 | $178.88 | $18.62 | $197.50 | | |
| 10 | Treatstock · PLA black, cheapest hub (Prozix, Long Beach) | $198.45 | $22.43 | $220.88 | | US hobby hubs price by print-hours; 30 other hubs from $211 to $1,100 |
| — | JLC3DP · default white SLA 9600 resin, sanded | $37.86 | $26.01 | $63.87 | | **cheapest thing on the internet for this job**, but white and glossy: you would spray the tower interior matte black; resin warp risk as above |
| — | PCBWay | — | — | — | — | quote page requires an account; not run |

## The pick

**Craftcloud, PETG, black, standard finish, 20 % infill, the Corvallis3D $76.86 offer.** It is a
US hub with a strong rating, the material is the one the assembly guide assumes, black PETG prints
matte enough for the tower interior (line it with black paper if it comes glossy), and the whole
set lands in about three weeks. If you want it in ten days, the same vendor's $94.48 express offer.

Craftcloud let me carry the configuration all the way to the vendor list without an account; to
order you sign in, add the $76.86 offer to the cart, and pay. In the order notes paste:
"tower: print upside down (camera plate on bed), no supports; frame: floor ring down; door: flat.
3 walls, 0.2 mm layers, matte black if available."

## What each site required

- **JLC3DP**: no account for quoting. Defaults every upload to white SLA resin; to get FDM or MJF
  you batch-edit, and the Save button is dead until a customs "Product Desc" cascader is filled
  (I used Office Appliance → Computer Enclosure, HS 847330). FDM offers ABS/ASA/PLA-P/PLA-C/PC-ABS
  and the CF/PEEK exotics, **no PETG**; a part must exceed 30 × 30 × 10 mm.
- **Craftcloud**: no account for quoting; a location/units dialog appears after upload. Material →
  finish → colour → offer list with named vendors, production and shipping split out.
- **Treatstock**: no account for quoting; offers are per hub, sorted by default relevance; black
  and PETG filters exist but the price is driven by print time, so it never gets cheap for a
  315 cm³ part.
- **PCBWay**: login wall before the 3D quote form.
