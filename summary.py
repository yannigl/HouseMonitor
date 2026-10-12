#!/usr/bin/env python3
"""Résumé matinal Home Assistant -> Telegram (stdlib uniquement)."""
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CFG = json.loads((Path(__file__).parent / "config.json").read_text())


def http(url, headers=None, data=None):
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def history(entity_id, hours):
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    qs = urllib.parse.urlencode({"filter_entity_id": entity_id, "minimal_response": "", "no_attributes": ""})
    url = f"{CFG['ha_url']}/api/history/period/{start.isoformat()}?{qs}"
    data = http(url, {"Authorization": f"Bearer {CFG['ha_token']}"})
    points = []
    for s in data[0] if data else []:
        try:
            ts = datetime.fromisoformat(s.get("last_changed") or s["last_updated"]).astimezone()
            points.append((ts, float(s["state"])))
        except (ValueError, KeyError):
            pass  # unavailable / unknown
    return points


def line(ent, hours):
    pts = history(ent["id"], hours)
    unit = ent.get("unit", "")
    if not pts:
        return f"• {ent['name']} : pas de données"
    lo = min(pts, key=lambda p: p[1])
    hi = max(pts, key=lambda p: p[1])
    fmt = lambda p: f"{p[1]:g}{unit} à {p[0]:%Hh%M}"
    return f"• {ent['name']} : min {fmt(lo)}, max {fmt(hi)}, actuel {pts[-1][1]:g}{unit}"


def main():
    hours = CFG.get("hours", 24)
    text = f"☀️ Résumé du {datetime.now():%d/%m} (dernières {hours} h)\n" + "\n".join(
        line(e, hours) for e in CFG["entities"]
    )
    if "--dry" in sys.argv:
        print(text)
        return
    http(
        f"https://api.telegram.org/bot{CFG['telegram_token']}/sendMessage",
        data=urllib.parse.urlencode({"chat_id": CFG["telegram_chat_id"], "text": text}).encode(),
    )


if __name__ == "__main__":
    main()
