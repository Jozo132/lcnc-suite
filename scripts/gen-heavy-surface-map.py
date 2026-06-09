#!/usr/bin/env python3
"""Generate a heavy surface-map test pair (probe-results.txt + probe-results-grid.json)
for stress-testing the 3D viewer's surface layer (F7: InstancedMesh probe dots +
the deferred out-of-hull nearest-neighbour fill).

The probe points are sampled on a DISK (a round part), so the square comp grid's
corners fall outside the convex hull and land as JSON `null` — which is exactly what
exercises the frontend's O(invalid-cells × points) NN fill. Pure stdlib (no numpy).

Files are written next to a LinuxCNC INI (the gateway reads them from there). The
gateway loads them at startup, so restart the suite after generating.

  python3 scripts/gen-heavy-surface-map.py            # heavy defaults into the sim config
  python3 scripts/gen-heavy-surface-map.py --grid 220 --step 2.0   # heavier
  python3 scripts/gen-heavy-surface-map.py --out-dir /path/to/config
"""
import argparse
import json
import math
import os


def surface_z(x: float, y: float) -> float:
    """A gentle, realistic-looking flatness warp in ~±2 mm (saddle + radial ripple)."""
    return (1.2 * math.sin(x / 28.0) * math.cos(y / 22.0)
            + 0.6 * math.sin(math.hypot(x, y) / 18.0))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", default="/home/cnc/linuxcnc/configs/lcnc_suite_sim",
                    help="config dir (where the INI lives); files go here")
    ap.add_argument("--radius", type=float, default=100.0, help="probe disk radius (mm)")
    ap.add_argument("--step", type=float, default=2.5, help="probe point spacing (mm)")
    ap.add_argument("--grid", type=int, default=160, help="comp-grid resolution per axis")
    ap.add_argument("--cx", type=float, default=0.0, help="disk centre X")
    ap.add_argument("--cy", type=float, default=0.0, help="disk centre Y")
    args = ap.parse_args()

    R, S, cx, cy = args.radius, args.step, args.cx, args.cy

    # --- probe points: a grid clipped to the disk (round part) ---
    pts = []
    n = int((2 * R) / S) + 1
    for iy in range(n):
        for ix in range(n):
            x = cx - R + ix * S
            y = cy - R + iy * S
            if (x - cx) ** 2 + (y - cy) ** 2 <= R * R:
                pts.append((x, y, surface_z(x - cx, y - cy)))

    txt_path = os.path.join(args.out_dir, "probe-results.txt")
    with open(txt_path, "w") as f:
        for x, y, z in pts:
            f.write(f"{x:.3f} {y:.3f} {z:.4f}\n")

    # --- comp grid: square over the disk's bounding box; null outside the disk ---
    nx = ny = max(2, args.grid)
    gx = [cx - R + i * (2 * R) / (nx - 1) for i in range(nx)]
    gy = [cy - R + j * (2 * R) / (ny - 1) for j in range(ny)]
    zi = []          # zi[ix][iy] — matches the frontend's grid.zi[ix][iy]
    null_cells = 0
    for ix in range(nx):
        col = []
        for iy in range(ny):
            x, y = gx[ix], gy[iy]
            if (x - cx) ** 2 + (y - cy) ** 2 <= R * R:
                col.append(round(surface_z(x - cx, y - cy), 4))
            else:
                col.append(None)   # out-of-hull → frontend NN-fills (the deferred path)
                null_cells += 1
        zi.append(col)

    json_path = os.path.join(args.out_dir, "probe-results-grid.json")
    with open(json_path, "w") as f:
        json.dump({"x": gx, "y": gy, "zi": zi, "method": 0}, f)

    nn_ops = null_cells * len(pts)
    print(f"  probe-results.txt      {len(pts):>6d} points  ({os.path.getsize(txt_path)//1024} KB)")
    print(f"  probe-results-grid.json {nx}x{ny} grid, {null_cells} null cells "
          f"({os.path.getsize(json_path)//1024} KB)")
    print(f"  → {len(pts)} InstancedMesh dots; NN fill ~{nn_ops/1e6:.1f}M ops on load")
    print(f"  written to {args.out_dir}")
    print("  RESTART the suite (gateway reads these at startup), then load the surface layer.")


if __name__ == "__main__":
    main()
