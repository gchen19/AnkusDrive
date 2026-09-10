import math
"""Datasheet-faithful reference bodies for the bought parts (rev 2.3). Exec'd by the build script
with box/cyl/V/Part and the layout constants in scope. Every dimension carries its source."""
SRC_REF = {
    "pi5":     "Raspberry Pi 5 mechanical drawing: 85 x 56 PCB, holes 3.5 from edges on 58 x 49; USB-C + 2x micro-HDMI on the "
               "long edge (USB-C centred 11.2 from the corner), 2x USB-A double stacks + RJ45 on the short edge; 40-pin header along the "
               "opposite long edge; two 22-pin MIPI connectors between HDMI and header side",
    "cooler":  "Raspberry Pi Active Cooler: extruded heatsink ~46 x 36 x 14 with 30 mm fan; overall ~20 mm above the PCB",
    "hat":     "Adafruit Perma-Proto HAT 2310: 65 x 56.5 x 1.6, 2x20 socket, camera-cable slot; tall stacking header 2x20 pins 19 mm",
    "hqcam":   "RPi HQ Camera drawing RP-008200: 38 x 38 PCB, holes d2.5 at 4 from edges, CS ring d30.75, housing d36, mount 12.04 "
               "from PCB back, tripod block 13.97 wide x 5.71 tall protruding 6.54 behind, 15-pin FPC at the top edge",
    "lens":    "Arducam 8 mm CS lens (LN-B0186?): d28 x 23 mm, focus ring + aperture ring, front element ~d12",
    "display": "Waveshare 2inch LCD Module: 58 x 35 mm outline, active 40.8 x 30.6, 8-pin 2.54 header, mounting holes at the corners (VERIFY spacing)",
    "button":  "Adafruit 3425 = PM192-11E/42RGB/12V/S datasheet: d19 hole (panel <= 10), bezel d22 x 1.8, M19x1, 38.2 mm behind the bezel, hex nut 25.2 across corners, 7 solder lugs, common-anode RGB ring",
    "pad":     "Huion L4S: 360 x 270 x 5 mm, lit area 310 x 210, touch switch on the front-left edge, micro-USB on the left edge",
    "plate":   "CELLTREAT 6 Well Plate drawing 7/2/20: 127.8 x 85.38 x 20.2, lid 127.0 x 84.8 x 9.9, wells d34.7/35.5 x 17.2, one chamfered corner",
    "cables":  "RPi 22-to-15-pin camera cable 300 mm: 12.6 mm wide at the Pi 5 end, 16 mm at the camera end; Cat6 d5.5 + RJ45 11.7 x 21 x 8; USB-C d4",
}

def rounded_box(x0, y0, z0, x1, y1, z1, r):
    """Box with vertical edges filleted (r), via 2D sketch extrude."""
    b = box(x0, y0, z0, x1, y1, z1)
    try:
        edges = [e for e in b.Edges if abs(e.tangentAt(e.FirstParameter).z) > 0.99]
        return b.makeFillet(r, edges)
    except Exception:
        return b

def path_tube(pts, r):
    """Cable along a polyline: cylinders between points + spheres at the joints."""
    segs = []
    for a, b_ in zip(pts[:-1], pts[1:]):
        d = b_ - a
        if d.Length < 1e-6: continue
        segs.append(Part.makeCylinder(r, d.Length, a, d))
        segs.append(Part.makeSphere(r, b_))
    s = segs[0]
    for x in segs[1:]: s = s.fuse(x)
    return s

def fillet_polyline(pts, r, n_arc=10):
    """Round every corner of a polyline with a circular arc of radius r (clamped to half the shorter
    neighbouring segment). Returns a dense list of points that a spline can pass through."""
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        p0, p1, p2 = pts[i - 1], pts[i], pts[i + 1]
        d0 = (p0 - p1); d2 = (p2 - p1)
        l0, l2 = d0.Length, d2.Length
        if l0 < 1e-6 or l2 < 1e-6: continue
        u0, u2 = d0 * (1 / l0), d2 * (1 / l2)
        cosang = max(-1.0, min(1.0, u0.dot(u2)))
        ang = math.acos(cosang)                      # angle between the two legs
        if ang > math.pi - 1e-3: continue            # straight through
        rr = min(r, 0.49 * l0, 0.49 * l2)
        tlen = rr / math.tan(ang / 2)                # tangent length from the corner
        a = p1 + u0 * tlen; b_ = p1 + u2 * tlen
        bis = (u0 + u2); bis.normalize()
        c = p1 + bis * (rr / math.sin(ang / 2))      # arc centre
        va = a - c; vb = b_ - c
        for k in range(n_arc + 1):
            t = k / n_arc
            # slerp between va and vb about c
            w = math.acos(max(-1.0, min(1.0, va.dot(vb) / (va.Length * vb.Length))))
            if w < 1e-6: q = va
            else: q = va * (math.sin((1 - t) * w) / math.sin(w)) + vb * (math.sin(t * w) / math.sin(w))
            out.append(c + q)
    out.append(pts[-1])
    return out

def fillet_segments(pts, r, min_r=0.0):
    """Polyline -> list of ("line", a, b) and ("arc", a, b, centre, axis, angle_rad) pieces, tangent-continuous.
    A corner whose fillet would be tighter than min_r is left sharp as ("joint", p)."""
    segs = []; cur = pts[0]
    for i in range(1, len(pts) - 1):
        p0, p1, p2 = pts[i - 1], pts[i], pts[i + 1]
        d0 = p0 - p1; d2 = p2 - p1; l0, l2 = d0.Length, d2.Length
        if l0 < 1e-6 or l2 < 1e-6: continue
        u0, u2 = d0 * (1 / l0), d2 * (1 / l2)
        ang = math.acos(max(-1.0, min(1.0, u0.dot(u2))))
        if ang > math.pi - 1e-3: continue
        rr = min(r, 0.49 * l0, 0.49 * l2)
        if rr < min_r:
            segs.append(("line", cur, p1)); segs.append(("joint", p1)); cur = p1; continue
        tlen = rr / math.tan(ang / 2)
        a = p1 + u0 * tlen; b_ = p1 + u2 * tlen
        bis = u0 + u2; bis.normalize()
        c = p1 + bis * (rr / math.sin(ang / 2))
        axis = u0.cross(u2); axis.normalize()
        segs.append(("line", cur, a)); segs.append(("arc", a, b_, c, axis, math.pi - ang)); cur = b_
    segs.append(("line", cur, pts[-1]))
    return segs

def sweep_round(pts, r, bend_r):
    """Cable of radius r along the filleted polyline: cylinders on the straights, revolved discs on the bends."""
    pieces = []
    for sg in fillet_segments(pts, bend_r, min_r=r + 0.3):
        if sg[0] == "joint":
            pieces.append(Part.makeSphere(r, sg[1]))
        elif sg[0] == "line":
            a, b_ = sg[1], sg[2]; d = b_ - a
            if d.Length > 1e-6: pieces.append(Part.makeCylinder(r, d.Length, a, d))
        else:
            a, b_, c, axis, ang = sg[1:]
            t = a - c; t.normalize(); u = axis.cross(t)                    # tangent at a, along the arc toward b
            disc = Part.Face(Part.Wire(Part.makeCircle(r, a, u)))
            best = None
            for sgn in (1, -1):
                rot = App.Rotation(axis, sgn * math.degrees(ang))
                if best is None or (c + rot.multVec(a - c) - b_).Length < best[0]: best = ((c + rot.multVec(a - c) - b_).Length, sgn)
            pieces.append(disc.revolve(c, axis * best[1], math.degrees(ang)))
    s = pieces[0]
    for x in pieces[1:]: s = s.fuse(x)
    return s.removeSplitter()

def flat_ribbon(pts, width, t, width_dir):
    """Flat cable along a polyline. The WIDTH always runs along width_dir (X for a cable lying on
    the top plate and down the rear wall); the thickness is perpendicular to both the run and the
    width. Segments overlap by t/2 at the joints and each bend gets a quarter-round so it reads as
    a real FPC bending over an edge."""
    segs = []
    wd = width_dir.normalize()
    for i, (a, b_) in enumerate(zip(pts[:-1], pts[1:])):
        d = b_ - a; L = d.Length
        if L < 1e-6: continue
        u = d.normalize(); n = u.cross(wd).normalize()
        a2 = a - u * (t / 2); L2 = L + t
        face = Part.Face(Part.makePolygon([a2 - wd * (width/2) - n * (t/2), a2 + wd * (width/2) - n * (t/2),
                                           a2 + wd * (width/2) + n * (t/2), a2 - wd * (width/2) + n * (t/2), a2 - wd * (width/2) - n * (t/2)]))
        segs.append(face.extrude(u * L2))
    s = segs[0]
    for x in segs[1:]: s = s.fuse(x)
    return s

# ---------------------------------------------------------------- Raspberry Pi 5 (+ cooler, HAT, header) — board in the XZ plane, components toward +Y
def make_pi(x0, y_pcb, z0):
    """x0,z0 = board lower-left corner; y_pcb = PCB back face (against the sled bosses)."""
    T = 1.6; yc = y_pcb + T                       # component face
    pcb = box(x0, y_pcb, z0, x0 + 85, yc, z0 + 56)
    for hx in (3.5, 61.5):
        for hz in (3.5, 52.5):
            pcb = pcb.cut(cyl(1.35, T + 1, x0 + hx, y_pcb - 0.5, z0 + hz, V(0, 1, 0)))
    parts = []
    parts.append(box(x0 + 6.7, yc, z0 - 1.5, x0 + 15.7, yc + 3.2, z0 + 1.5))                        # USB-C, centred 11.2 from the corner, overhangs the edge 1.5
    for hx in (25.8, 39.2):                                                                          # 2x micro-HDMI
        parts.append(box(x0 + hx - 3.5, yc, z0 - 1.0, x0 + hx + 3.5, yc + 3.0, z0 + 2.0))
    parts.append(box(x0 + 42.5, yc, z0 - 0.5, x0 + 55.5, yc + 2.5, z0 + 4.0))                           # CAM/DISP 1, the 22-pin MIPI connector next to HDMI1 (ribbon centred x0+49)
    parts.append(box(x0 + 85 - 21.5, yc, z0 + 2.25, x0 + 85 + 3.0, yc + 13.5, z0 + 18.25))            # RJ45: Pi 5 puts it in the corner next to the USB-C edge (centre 10.25 up)
    parts.append(box(x0 + 85 - 2, yc, z0 + 20.4, x0 + 85 + 4.5, yc + 16.0, z0 + 33.6))                 # USB-A double stack A, centre 27 (overhangs the short edge 4.5)
    parts.append(box(x0 + 85 - 2, yc, z0 + 38.4, x0 + 85 + 4.5, yc + 16.0, z0 + 51.6))                 # USB-A double stack B, centre 45
    parts.append(box(x0 + 7.1, yc, z0 + 56 - 3.5 - 5.1, x0 + 7.1 + 50.8, yc + 8.5, z0 + 56 - 3.5 + 2.5))  # 2x20 header along the top long edge
    parts.append(box(x0 + 22, yc, z0 + 10, x0 + 22 + 17, yc + 2.5, z0 + 10 + 17))                     # SoC
    # Active Cooler: heatsink over the SoC/PMIC area + 30 mm fan
    parts.append(box(x0 + 14, yc + 2.5, z0 + 6, x0 + 14 + 46, yc + 2.5 + 12, z0 + 6 + 36))
    parts.append(cyl(15, 7, x0 + 14 + 46 - 17, yc + 14.5, z0 + 6 + 18, V(0, 1, 0)))
    pi = pcb
    for p in parts: pi = pi.fuse(p)
    # tall stacking header + Perma-Proto HAT
    hdr = box(x0 + 7.1, yc + 8.5, z0 + 56 - 3.5 - 5.1, x0 + 7.1 + 50.8, yc + 8.5 + 19.0, z0 + 56 - 3.5 + 2.5)
    hy = yc + 8.5 + 19.0 - 8.5
    hat = box(x0 + 10, hy + 8.5, z0, x0 + 10 + 65, hy + 8.5 + 1.6, z0 + 56.5)
    hat = hat.cut(box(x0 + 41, hy + 8, z0 - 1, x0 + 41 + 20, hy + 11, z0 + 8))                        # camera-cable slot
    # side-entry JST-XH sockets (S8B-XH-A / S5B-XH-A, 7.0 tall): the 8-way opens toward -X, the 5-way toward +X,
    # so both pigtails run along the HAT surface and neither has to bend away from the control face 2.3 mm in front
    hat = hat.fuse(box(x0 + 38, hy + 10.1, z0 + 20, x0 + 38 + 20, hy + 10.1 + 7, z0 + 20 + 6))        # 8-way, display; entry face at x0+38
    hat = hat.fuse(box(x0 + 60.5, hy + 10.1, z0 + 32, x0 + 60.5 + 12.5, hy + 10.1 + 7, z0 + 32 + 6))  # 5-way, button; entry face at x0+73
    hat = hat.fuse(box(x0 + 44, hy + 10.1, z0 + 40, x0 + 44 + 19.3, hy + 10.1 + 4.0, z0 + 40 + 6.4))  # ULN2003A DIP-16
    for hx in (25, 33, 41):                                                                          # three 220 ohm resistors
        hat = hat.fuse(cyl(1.1, 6, x0 + hx, hy + 10.1 + 1.2, z0 + 36, V(0, 0, 1)))
    return pi, hdr.fuse(hat)

# ---------------------------------------------------------------- HQ camera + 8 mm lens (board in the XY plane, sensor at z_sensor, lens hangs -Z)
def make_hq(z_sensor):
    T = 1.6; zb = z_sensor - T
    pcb = box(-19, -19, zb, 19, 19, z_sensor)
    for hx in (-15, 15):
        for hy in (-15, 15):
            pcb = pcb.cut(cyl(1.25, T + 1, hx, hy, zb - 0.5))
    housing = cyl(18, 5.0, 0, 0, z_sensor).fuse(cyl(15.4, 12.04 - 5.0, 0, 0, z_sensor + 5.0))       # d36 base ring + CS mount to 12.04 above the PCB back... (mount faces -Z in our frame: flip)
    # in our frame the lens looks DOWN: the mount sits on the -Z side of the board (board above the plate)
    housing = cyl(18, 5.0, 0, 0, zb - 5.0).fuse(cyl(15.4, 7.04, 0, 0, zb - 12.04))
    tripod = box(-6.985, -19 - 6.54, zb - 5.71, 6.985, -19, zb)                                     # tripod block behind (here: below) the board edge
    fpc = box(-8.5, 19 - 5.5, z_sensor, 8.5, 19 + 0.8, z_sensor + 3.0)                              # 15-pin FPC connector at the rear edge
    cam = pcb.fuse(housing).fuse(tripod).fuse(fpc)
    lens = cyl(14.0, 23.0, 0, 0, zb - 12.04 - 23.0)                                                  # Arducam 8 mm: d28 x 23
    lens = lens.cut(cyl(6.0, 2.0, 0, 0, zb - 12.04 - 23.0 - 0.01)).fuse(cyl(14.6, 4.0, 0, 0, zb - 12.04 - 9.0)).fuse(cyl(14.6, 4.0, 0, 0, zb - 12.04 - 20.0))   # front element recess, focus + aperture rings
    return cam, lens

# ---------------------------------------------------------------- 2" display module + 19 mm button
def make_display(cx, cz, y_front_face, hsg_t):
    """Module PCB 58 x 35, glass 46 x 34 with active 40.8 x 30.6, header 8 x 2.54 along the bottom edge."""
    yg = y_front_face + hsg_t                                                                       # inner face of the bezel wall
    pcb = box(cx - 29, yg + 5.0, cz - 17.5, cx + 29, yg + 6.6, cz + 17.5)                          # PCB sits on the 5 mm bosses
    for hx in (-26, 26):
        for hz in (-15, 15):
            pcb = pcb.cut(cyl(1.1, 3, cx + hx, yg + 4.5, cz + hz, V(0, 1, 0)))
    glass = box(cx - 23, yg + 2.6, cz - 17, cx + 23, yg + 5.0, cz + 17)                            # LCD glass on the PCB front, 2.6 mm behind the bezel face
    hdr = box(cx - 10.2, yg + 6.6, cz - 17.5 + 1.5, cx + 10.2, yg + 6.6 + 6.0, cz - 17.5 + 4.0)     # 8-pin header on the back
    pins = box(cx - 9.5, yg + 6.6 + 6.0, cz - 17.5 + 2.2, cx + 9.5, yg + 6.6 + 11.0, cz - 17.5 + 3.2)
    return glass.fuse(pcb).fuse(hdr).fuse(pins)

def make_button(bx, bz, y_panel):
    """PM192-11E/42RGB (Adafruit 3425): bezel d22 x 1.8, M19x1 thread, 38.2 behind the bezel, hex nut 25.2 A/C,
    7 solder lugs (NC1 NC2 C1 C2, Red Green Blue, C+), IP67 seal ring d16."""
    bezel = cyl(11.0, 1.8, bx, y_panel - 1.8, bz, V(0, 1, 0)).cut(cyl(8.6, 1.9, bx, y_panel - 1.85, bz, V(0, 1, 0)))
    dome = cyl(8.4, 1.2, bx, y_panel - 1.2, bz, V(0, 1, 0)).fuse(cyl(7.2, 2.2, bx, y_panel - 2.2, bz, V(0, 1, 0)))     # slightly domed cap, 2 mm stroke
    ring = cyl(9.6, 0.5, bx, y_panel - 1.85, bz, V(0, 1, 0)).cut(cyl(8.6, 0.6, bx, y_panel - 1.9, bz, V(0, 1, 0)))
    body = cyl(9.5, 38.2, bx, y_panel, bz, V(0, 1, 0))                                                                 # M19 body, 38.2 long
    nut = Part.makePolygon([V(bx + 12.6 * math.cos(math.radians(a)), y_panel + WALL, bz + 12.6 * math.sin(math.radians(a))) for a in range(0, 361, 60)])
    nut = Part.Face(nut).extrude(V(0, 4.0, 0)).cut(cyl(9.6, 5, bx, y_panel + WALL - 0.5, bz, V(0, 1, 0)))
    tail = cyl(8.0, 6.0, bx, y_panel + 38.2, bz, V(0, 1, 0))                                                          # PBT base
    lugs = []
    for i, (dx, dz) in enumerate([(-6, 5), (0, 5), (6, 5), (-6, -5), (0, -5), (6, -5), (0, 0)]):
        lugs.append(box(bx + dx - 1.0, y_panel + 44.2, bz + dz - 0.4, bx + dx + 1.0, y_panel + 48.5, bz + dz + 0.4))
    b = bezel.fuse(dome).fuse(body).fuse(nut).fuse(tail)
    for t in lugs: b = b.fuse(t)
    return b, ring

# ---------------------------------------------------------------- Huion L4S pad + CELLTREAT plate
def make_pad():
    pad = rounded_box(-180, -135, -5, 180, 135, 0, 6)
    lit = box(-155, -105, -0.3, 155, 105, 0.0)
    touch = cyl(5, 0.3, -150, -120, 0.0)
    port = box(-181, 60, -4, -178, 68, -1)
    return pad.cut(lit.common(pad)).fuse(lit), lit, touch, port

def make_plate(L, W, H, lid_L, lid_W, lid_H, well_id, pitch, depth):
    body = box(-L/2, -W/2, 0, L/2, W/2, H - 2.8)
    skirt = box(-L/2 + 1.2, -W/2 + 1.2, -0.01, L/2 - 1.2, W/2 - 1.2, 3.0 - 1.5)
    body = body.cut(skirt)                                                                           # hollow skirt under the well floors
    for wx in (-pitch, 0, pitch):
        for wy in (-pitch/2, pitch/2):
            body = body.cut(cyl(well_id/2, depth + 1, wx, wy, 3.0))
    ch = Part.makePolygon([V(L/2, -W/2, -1), V(L/2 - 4, -W/2, -1), V(L/2, -W/2 + 4, -1), V(L/2, -W/2, -1)])
    body = body.cut(Part.Face(ch).extrude(V(0, 0, H + 2)))                                           # chamfered corner (front-right in the drawing)
    lid = box(-lid_L/2, -lid_W/2, H - lid_H, lid_L/2, lid_W/2, H).cut(box(-lid_L/2 + 1.2, -lid_W/2 + 1.2, H - lid_H - 1, lid_L/2 - 1.2, lid_W/2 - 1.2, H - 1.5))
    for wx in (-pitch, 0, pitch):                                                                    # condensation rings on the lid underside
        for wy in (-pitch/2, pitch/2):
            lid = lid.fuse(cyl(19.6, 1.5, wx, wy, H - 3.0).cut(cyl(19.2, 1.6, wx, wy, H - 3.05)))
    lid = lid.cut(Part.Face(ch).extrude(V(0, 0, H + 2)))
    return body.fuse(lid).removeSplitter()

def make_display_inside(cx, cz, y_plate_in, y_plate_out):
    """Waveshare 2" module screwed to bosses on the INSIDE of a plate: glass toward the window (+Y), PCB and header inward (-Y)."""
    yg = y_plate_in - 5.0                                                                            # boss height 5: PCB back face here
    pcb = box(cx - 29, yg - 1.6, cz - 17.5, cx + 29, yg, cz + 17.5)
    for hx in (-26, 26):
        for hz in (-15, 15):
            pcb = pcb.cut(cyl(1.1, 3, cx + hx, yg - 2.0, cz + hz, V(0, 1, 0)))
    glass = box(cx - 23, yg, cz - 17, cx + 23, yg + 2.4, cz + 17)                                   # glass on the PCB front, 2.6 mm behind the plate's outer face
    hdr = box(cx - 10.2, yg - 1.6 - 6.0, cz - 17.5 + 1.5, cx + 10.2, yg - 1.6, cz - 17.5 + 4.0)
    pins = box(cx - 9.5, yg - 1.6 - 11.0, cz - 17.5 + 2.2, cx + 9.5, yg - 1.6 - 6.0, cz - 17.5 + 3.2)
    return glass.fuse(pcb).fuse(hdr).fuse(pins)
