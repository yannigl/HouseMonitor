"""Graphique PNG des températures sur la fenêtre du résumé, en bibliothèque standard seulement (pas de matplotlib).

Une courbe de couleur par capteur, avec tous les relevés disponibles ; les nombres des axes sont dessinés avec une police de chiffres
intégrée. La légende (couleurs, minimum, maximum, valeur actuelle) est dans la légende du message Telegram : une pastille de couleur
par capteur, de la même couleur que la courbe.
"""
import math
import struct
import zlib
from datetime import timedelta

W = 1000
LEFT, RIGHT, TOP, BOTTOM = 64, 18, 18, 44
RIGHT_WIDE = 62  # marge droite quand un graphique a un second axe (direction du vent)
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
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"], "i": ["00100", "00000", "01100", "00100", "00100", "00100", "01110"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"], "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "p": ["00000", "00000", "11110", "10001", "11110", "10000", "10000"], "V": ["10001", "10001", "10001", "10001", "01010", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"], "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"], "z": ["00000", "00000", "11111", "00010", "00100", "01000", "11111"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"], "x": ["00000", "00000", "10001", "01010", "00100", "01010", "10001"],
    "d": ["00001", "00001", "01101", "10011", "10001", "10011", "01101"], "u": ["00000", "00000", "10001", "10001", "10001", "10011", "01101"],
    "e": ["00000", "00000", "01110", "10001", "11111", "10000", "01110"], "n": ["00000", "00000", "10110", "11001", "10001", "10001", "10001"],
    "k": ["10000", "10010", "10100", "11000", "10100", "10010", "10001"], "m": ["00000", "00000", "11010", "10101", "10101", "10101", "10101"],
    "s": ["00000", "00000", "01111", "10000", "01110", "00001", "11110"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
    "%": ["11001", "11010", "00010", "00100", "01000", "01011", "10011"], "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "h": ["10000", "10000", "10110", "11001", "10001", "10001", "10001"], "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "a": ["00000", "00000", "01110", "00001", "01111", "10001", "01111"], "°": ["01100", "10010", "10010", "01100", "00000", "00000", "00000"],
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

    def band(self, cols, color, alpha=0.24):
        """Zone entre une courbe haute et une courbe basse : `cols` = [(x, y_haut, y_bas)] triés par x, interpolés colonne par colonne."""
        if len(cols) == 1:
            cols = [(cols[0][0] - 2, cols[0][1], cols[0][2]), (cols[0][0] + 2, cols[0][1], cols[0][2])]
        i = 0
        for x in range(int(cols[0][0]), int(cols[-1][0]) + 1):
            while i < len(cols) - 2 and cols[i + 1][0] < x:
                i += 1
            (xa, ha, la), (xb, hb, lb) = cols[i], cols[i + 1]
            f = 0.0 if xb == xa else max(0.0, min(1.0, (x - xa) / (xb - xa)))
            top, bottom = ha + (hb - ha) * f, la + (lb - la) * f
            for y in range(int(top), int(bottom) + 1):
                self.blend(x, y, color, alpha)

    def polyline(self, pp, color, width=2.2, dash=None):
        """Courbe passant par les points `pp` ; `dash` = (trait, espace) en pixels pour des pointillés (le motif continue d'un segment à l'autre)."""
        phase = 0.0
        for (ax, ay), (bx, by) in zip(pp, pp[1:]):
            if not dash:
                self.line(ax, ay, bx, by, color, width)
                continue
            seg = math.hypot(bx - ax, by - ay)
            on, off = dash
            pos = 0.0
            while pos < seg:
                p = phase % (on + off)
                run = min((on - p) if p < on else (on + off - p), seg - pos)
                if p < on and run > 0:
                    f0, f1 = pos / seg, (pos + run) / seg
                    self.line(ax + (bx - ax) * f0, ay + (by - ay) * f0, ax + (bx - ax) * f1, ay + (by - ay) * f1, color, width)
                pos += max(run, 0.5)
                phase += max(run, 0.5)

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


def time_ticks(start, end):
    """Repères de l'axe du temps selon la durée : toutes les 3 h (HH:MM) jusqu'à 36 h, chaque jour ou tous les N jours (JJ/MM) jusqu'à 70 jours,
    puis chaque mois (MM/AA). -> [(datetime, étiquette)]."""
    span = (end - start).total_seconds() / 86400
    out = []
    if span <= 1.5:
        t = start.replace(minute=0, second=0, microsecond=0)
        while t <= end:
            if t >= start and t.hour % 3 == 0:
                out.append((t, f"{t:%H:%M}"))
            t += timedelta(hours=1)
    elif span <= 70:
        step = next(k for k in (1, 2, 3, 5, 7, 10, 14, 30) if span / k <= 9)
        t = start.replace(hour=0, minute=0, second=0, microsecond=0)
        while t <= end:
            if t >= start and t.toordinal() % step == 0:
                out.append((t, f"{t:%d/%m}"))
            t += timedelta(days=1)
    else:
        t = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while t <= end:
            if t >= start:
                out.append((t, f"{t:%m/%y}"))
            t = (t.replace(day=28) + timedelta(days=4)).replace(day=1)
        if len(out) > 12:
            out = out[::2]
    return out


def runs(points, is_gap):
    """Découpe une liste de points [(t, valeur)] en tronçons continus : une valeur « trou » (is_gap) sépare deux tronçons."""
    out, cur = [], []
    for t, v in points:
        if is_gap(v):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append((t, v))
    if cur:
        out.append(cur)
    return out


PANEL_H, PANEL_GAP = 290, 24
WIDTH_NORMAL, WIDTH_BOLD, DASH = 2.2, 4.8, (11, 7)


def draw_right_axis(c, panel, px, y0, y1, x1):
    """Second axe (à droite) : graduations et points d'une série, ex. la direction du vent de 0 à 360°."""
    r = panel["right"]
    lo, hi = r["range"]
    py = lambda v: y1 - (v - lo) / (hi - lo) * (y1 - y0)
    for v in range(int(lo), int(hi) + 1, 90):
        y = int(py(v))
        c.rect(x1, y, x1 + 4, y, r["color"])
        c.text(x1 + 8, y - 7, f"{v}", color=r["color"])
    c.text(x1 + 8, y0 - 18, r["unit"], color=r["color"], scale=2)
    for t, v in r["points"]:
        if v is not None:
            x, y = int(px(t)), int(py(v))
            c.rect(x - 1, y - 1, x + 1, y + 1, r["color"])


def render(panels, start, end):
    """Plusieurs graphiques les uns sous les autres, sur le même axe de temps.
    `panels` : [{"unit": "°C", "series": [(couleur RVB, [(datetime, valeur), ...], gras, pointillés)], "ref": repère ou None, "clamp": (min, max) ou None}].
    -> octets PNG."""
    panels = [p for p in panels if p["series"] or p.get("right")]
    ph = PANEL_H if len(panels) <= 3 else 250  # quatre graphiques : un peu moins hauts
    height = len(panels) * (ph + PANEL_GAP) + BOTTOM
    c = Canvas(W, height)
    x0, x1 = LEFT, W - (RIGHT_WIDE if any(p.get("right") for p in panels) else RIGHT)
    total = (end - start).total_seconds()
    px = lambda t: x0 + (t - start).total_seconds() / total * (x1 - x0)
    grid, axis = (225, 225, 225), (120, 120, 120)
    ticks = time_ticks(start, end)
    for n, panel in enumerate(panels):
        y0 = n * (ph + PANEL_GAP) + PANEL_GAP
        y1 = y0 + ph - 6
        vals = [v for entry in panel["series"] for _, v in entry[1] if v is not None]
        vals += [v for entry in panel["series"] if len(entry) > 4 and entry[4] for _, lo, hi in entry[4] if lo is not None for v in (lo, hi)]
        if panel.get("ref") is not None:
            vals.append(panel["ref"])
        if panel.get("floor") is not None:
            vals.append(panel["floor"])
        if not vals:
            vals = [0.0, 1.0]
        lo, hi = min(vals), max(vals)
        step = nice_step(max(hi - lo, 2.0), 5)
        ymin, ymax = math.floor((lo - 0.3) / step) * step, math.ceil((hi + 0.3) / step) * step
        if panel.get("floor") is not None:
            ymin = max(ymin, panel["floor"])
        if panel.get("clamp"):
            ymin, ymax = max(ymin, panel["clamp"][0]), min(ymax, panel["clamp"][1])
        py = lambda v: y1 - (v - ymin) / (ymax - ymin) * (y1 - y0)
        v = ymin
        while v <= ymax + 1e-9:  # lignes horizontales et valeurs
            y = int(py(v))
            c.rect(x0, y, x1, y, grid)
            c.text(x0 - 8, y - 7, f"{v:g}", anchor="right")
            v += step
        for t, label in ticks:  # lignes verticales ; les étiquettes sont écrites sous le dernier graphique
            x = int(px(t))
            c.rect(x, y0, x, y1, grid)
            if n == len(panels) - 1:
                c.text(x, y1 + 10, label, anchor="center")
        c.rect(x0, y0, x0, y1, axis)
        c.rect(x0, y1, x1, y1, axis)
        c.rect(x0 + 3, y0 + 2, x0 + 8 + len(panel["unit"]) * 18 + 4, y0 + 30, (255, 255, 255))  # fond blanc : l'unité reste lisible sous une courbe
        c.text(x0 + 8, y0 + 4, panel["unit"], color=(40, 40, 40), scale=3)
        if panel.get("ref") is not None and ymin <= panel["ref"] <= ymax:  # repère (1013 hPa) : pointillés sombres
            yr = py(panel["ref"])
            c.polyline([(x0, yr), (x1, yr)], (60, 60, 60), 1.6, (6, 5))
            c.text(x1 - 6, int(yr) - 16, f"{panel['ref']:g}", color=(60, 60, 60), anchor="right")
        for entry in panel["series"]:
            color, points, bold, dashed = entry[:4]
            if len(entry) > 4 and entry[4]:  # plage minimum-maximum du jour : zone translucide, les chevauchements restent visibles
                for run in runs([(t, (lo, hi)) for t, lo, hi in entry[4]], lambda v: v[0] is None):  # un trou de données coupe la zone
                    c.band([(px(t), py(max(ymin, min(ymax, hi))), py(max(ymin, min(ymax, lo)))) for t, (lo, hi) in run], color)
            if len(entry) > 5 and entry[5]:  # nuage de points (rafales) : un petit carré par relevé, pas de courbe
                for t, v in points:
                    if v is not None:
                        x, y = int(px(t)), int(py(max(ymin, min(ymax, v))))
                        c.rect(x - 2, y - 2, x + 2, y + 2, color)
                continue
            for run in runs(points, lambda v: v is None):  # idem pour la courbe
                c.polyline([(px(t), py(max(ymin, min(ymax, v)))) for t, v in run], color, WIDTH_BOLD if bold else WIDTH_NORMAL,
                           (DASH if dashed is True else dashed) or None)
        if panel.get("right"):
            draw_right_axis(c, panel, px, y0, y1, x1)
    return c.png()
