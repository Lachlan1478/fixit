"""Render a URL as an ASCII QR code in the terminal.

Used by Start-Claude-Agent.bat so the phone can scan-to-connect instead of
typing the access token. Reconfigures stdout to UTF-8 first because the block
glyphs the QR is drawn with are not encodable on the legacy Windows (cp1252)
console.

Usage:  python qr.py "http://host:8007/?token=..."
"""

import sys

try:
    # Python 3.7+: switch the console stream to UTF-8 so █ / ▀ render.
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

try:
    import qrcode
except ImportError:
    # Non-fatal: the launcher still prints the tap-able link as plain text.
    sys.exit(0)


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        return 0
    url = sys.argv[1].strip()
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make()
    try:
        qr.print_ascii(invert=True)
    except Exception:
        # If the console still can't render blocks, fail quietly — the link
        # is printed by the launcher regardless.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
