"""Graphique PNG des températures sur la fenêtre du résumé, en bibliothèque standard seulement (pas de matplotlib).

Une courbe de couleur par capteur, avec tous les relevés disponibles ; les nombres des axes sont dessinés avec une police de chiffres
intégrée. La légende (couleurs, minimum, maximum, valeur actuelle) est dans la légende du message Telegram : une pastille de couleur
par capteur, de la même couleur que la courbe.
"""
import math
import struct
import zlib
from datetime import timedelta

W, H = 1000, 560
LEFT, RIGHT, TOP, BOTTOM = 64, 18, 18, 44
# (pastille Telegram, couleur RVB) : les 8 premiers capteurs ont une courbe
PALETTE = [("🔴", (220, 50, 47)), ("🔵", (38, 139, 210)), ("🟢", (46, 160, 67)), ("🟠", (245, 135, 0)),
           ("🟣", (140, 70, 190)), ("🟤", (140, 90, 50)), ("🟡", (215, 185, 0)), ("⚫", (45, 45, 45))]

# police 5x7 : 0-9 : . -
GLYPHS = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"], "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"], "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"], "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"], "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"], "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    ":": ["00000", "00100", "00100", "00000", "00100", "00100", "00000"], ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
}


class Canvas:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.px = bytearray(b"\xff" * (w * h * 3))

    def blend(self, x, y, color, a=1.0):
        if 0 <= x < self.w and 0 <= y < self.h and a > 0:
            i = (y * self.w + x) * 3
            for k in range(3):
                self.px[i + k] = int(self.px[i + k] * (1 - a) + color[k] * a)

    def rect(self, x0, y0, x1, y1, color):
        for y in range(max(0, y0), min(self.h, y1 + 1)):
            for x in range(max(0, x0), min(self.w, x1 + 1)):
                self.blend(x, y, color)

    def line(self, x0, y0, x1, y1, color, width=2.2):
        """Segment lissé : chaque pixel proche du segment est mélangé selon sa distance (épaisseur `width`)."""
        dx, dy = x1 - x0, y1 - y0
        length2 = dx * dx + dy * dy
        r = width / 2 + 1
        for y in range(int(min(y0, y1) - r), int(max(y0, y1) + r) + 1):
            for x in range(int(min(x0, x1) - r), int(max(x0, x1) + r) + 1):
                t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / length2))
                d = math.hypot(x - (x0 + t * dx), y - (y0 + t * dy))
                self.blend(x, y, color, max(0.0, min(1.0, width / 2 + 0.5 - d)))

    def text(self, x, y, s, color=(70, 70, 70), scale=2, anchor="left"):
        width = len(s) * 6 * scale
        if anchor == "right":
            x -= width
        elif anchor == "center":
            x -= width // 2
        for ch in s:
            for row, bits in enumerate(GLYPHS.get(ch, [])):
                for col, b in enumerate(bits):
                    if b == "1":
                        self.rect(x + col * scale, y + row * scale, x + (col + 1) * scale - 1, y + (row + 1) * scale - 1, color)
            x += 6 * scale

    def png(self):
        raw = b"".join(b"\x00" + bytes(self.px[y * self.w * 3:(y + 1) * self.w * 3]) for y in range(self.h))

        def chunk(tag, data):
            body = tag + data
            return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def nice_step(span, target=6):
    raw = span / target
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def render(series, start, end, hour_fmt=lambda t: f"{t:%H:%M}"):
    """`series` : [(couleur RVB, [(datetime, valeur), ...])] ; `start`, `end` : bornes de l'axe du temps. -> octets PNG."""
    pts = [v for _, p in series for _, v in p]
    lo, hi = min(pts), max(pts)
    step = nice_step(max(hi - lo, 2.0))
    ymin, ymax = math.floor((lo - 0.3) / step) * step, math.ceil((hi + 0.3) / step) * step
    c = Canvas(W, H)
    x0, x1, y0, y1 = LEFT, W - RIGHT, TOP, H - BOTTOM
    total = (end - start).total_seconds()
    px = lambda t: x0 + (t - start).total_seconds() / total * (x1 - x0)
    py = lambda v: y1 - (v - ymin) / (ymax - ymin) * (y1 - y0)
    grid, axis = (225, 225, 225), (120, 120, 120)
    v = ymin
    while v <= ymax + 1e-9:  # lignes horizontales et valeurs
        y = int(py(v))
        c.rect(x0, y, x1, y, grid)
        c.text(x0 - 8, y - 7, f"{v:g}", anchor="right")
        v += step
    first = start.replace(minute=0, second=0, microsecond=0)
    t = first
    while t <= end:  # lignes verticales toutes les 3 heures
        if t >= start and t.hour % 3 == 0:
            x = int(px(t))
            c.rect(x, y0, x, y1, grid)
            c.text(x, y1 + 10, hour_fmt(t), anchor="center")
        t = t + timedelta(hours=1)
    c.rect(x0, y0, x0, y1, axis)
    c.rect(x0, y1, x1, y1, axis)
    for color, points in series:
        pp = [(px(t), py(v)) for t, v in points]
        for (ax, ay), (bx, by) in zip(pp, pp[1:]):
            c.line(ax, ay, bx, by, color)
    return c.png()
