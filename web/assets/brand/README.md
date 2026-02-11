# Brand assets

Logo mark (stacked layers + cap) and lockups for **WatchTurm Control Room**.

**Logo mark**: Top dot + three stacked chevrons (small → middle → bottom). Use `symbol.svg` with `fill="currentColor"` and theme the wrapper: dark `color: #fff`, light `color: #0f172a` (slate-900). App uses inline SVG; `symbol-dark` / `symbol-light` are explicit-fill variants for exports.

## Files

| File | Use |
|------|-----|
| `symbol-dark.svg` | Mark for dark theme (sidebar, etc.) |
| `symbol-light.svg` | Mark for light theme |
| `symbol-favicon.svg` | Single-color mark for favicon |
| `logo-lockup-dark.svg` | Mark + "WatchTurm" / "Control Room" (dark) |
| `logo-lockup-light.svg` | Mark + "WatchTurm" / "Control Room" (light) |

## PNG export

To generate PNGs (favicon fallbacks, sharing):

1. **Script**: `pip install svglib reportlab pillow` then `python web/scripts/export_logo_pngs.py`.  
   Note: `renderPM` may require Cairo on your platform (e.g. Linux; on Windows you may need Cairo/GTK).
2. **Manual**: Export from the SVGs using Inkscape, Figma, or similar.

Outputs: `logo-mark-32.png`, `logo-mark-64.png`, `logo-mark-128.png`, `logo-lockup-512.png` in `assets/brand/`; `favicon-16.png`, `favicon-32.png`, `apple-touch-icon.png` in `web/`.  
`favicon.ico` can be created from `favicon-32.png` (e.g. via ImageMagick or an ico generator) if needed.
