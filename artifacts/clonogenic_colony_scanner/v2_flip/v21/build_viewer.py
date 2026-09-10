"""Embed the STLs + parts.json into viewer_template.html -> scratchpad/plate_scanner_explorer.html"""
import base64, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "plate_scanner_explorer.html")
names = ["pad","pad_lit","frame","door","magnets","plate","tower","cam_board","lens","cam_cover","screws","pi","hat","pi_sled","button","display","screen","plugs"]
stl = {n: base64.b64encode(open(os.path.join(HERE, n + ".stl"), "rb").read()).decode() for n in names if os.path.exists(os.path.join(HERE, n + ".stl"))}
meta = json.load(open(os.path.join(HERE, "parts.json")))
cables = json.load(open(os.path.join(HERE, "cables.json")))
data = "window.SCANNER_DATA=" + json.dumps({"stl": stl, "meta": meta, "cables": cables}) + ";"
html = open(os.path.join(HERE, "viewer_template.html")).read().replace("/*__DATA__*/", data)
open(out, "w").write(html); print(out, len(html) // 1024, "KB")
