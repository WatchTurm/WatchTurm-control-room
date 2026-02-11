#!/usr/bin/env python3
"""Export logo SVGs to PNGs. Requires: pip install svglib reportlab pillow."""
from pathlib import Path

try:
    from svglib.svglib import svg2rlg
    from reportlab.graphics import renderPM
except ImportError:
    print("Install: pip install svglib reportlab pillow")
    raise SystemExit(1)

WEB = Path(__file__).resolve().parents[1]
BRAND = WEB / "assets" / "brand"
MARK_DARK = BRAND / "symbol-dark.svg"
LOCKUP_DARK = BRAND / "logo-lockup-dark.svg"
FAVICON = BRAND / "symbol-favicon.svg"


def export(svg_path: Path, png_path: Path, w: int, h: int | None = None) -> None:
    h = h or w
    d = svg2rlg(str(svg_path))
    if d is None:
        raise SystemExit(f"Failed to load {svg_path}")
    scale_x = w / d.width
    scale_y = h / d.height
    d.width = w
    d.height = h
    d.scale = max(scale_x, scale_y)
    renderPM.drawToFile(d, str(png_path), fmt="PNG")
    print(f"  {png_path.name}")


def main() -> None:
    BRAND.mkdir(parents=True, exist_ok=True)
    print("Exporting logo mark PNGs...")
    export(MARK_DARK, BRAND / "logo-mark-32.png", 32)
    export(MARK_DARK, BRAND / "logo-mark-64.png", 64)
    export(MARK_DARK, BRAND / "logo-mark-128.png", 128)
    print("Exporting logo lockup...")
    export(LOCKUP_DARK, BRAND / "logo-lockup-512.png", 512, 102)
    print("Exporting favicon-sized marks to web/...")
    export(FAVICON, WEB / "favicon-16.png", 16)
    export(FAVICON, WEB / "favicon-32.png", 32)
    export(FAVICON, WEB / "apple-touch-icon.png", 180)
    print("Done.")


if __name__ == "__main__":
    main()
