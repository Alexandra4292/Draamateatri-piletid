"""
Draamateatri piletiteavitus.
Loeb Draamateatri mängukava ja saadab telefoni (ntfy) teate, kui:
  1) välja müüdud etendusele ilmuvad piletid (tagastused),
  2) müüki tuleb uus etendus,
  3) mängukavasse lisandub uus lavastus.
"""
import json
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

STATE_FILE = "state.json"
CONFIG_FILE = "config.json"
TZ = ZoneInfo("Europe/Tallinn")
HEADERS = {"User-Agent": "Mozilla/5.0 (isiklik piletiteavitus)"}
DEFAULT_PAGE = "https://www.draamateater.ee/mangukava/"
RENOTIFY_HOURS = 3  # sama etenduse kohta ei saadeta teadet sagedamini

DATE_RE = re.compile(r"^(?:[^\W\d]{1,3}\.?\s+)?(\d{2})\.(\d{2})(?![.\d])")
TIME_RE = re.compile(r"^(\d{1,2})[:.](\d{2})$")
PID_RE = re.compile(r"lavastus/p/(\d+)")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def full_date(day, month, now):
    year = now.year if int(month) >= now.month else now.year + 1
    return f"{day}.{month}.{year}"


def parse_schedule(html, now):
    """Tagastab kõik mängukava etendused:
    {võti: {"pid", "title", "date", "time", "link"}}; link on None, kui pileteid pole."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    events, date, ev = [], None, None
    for el in soup.descendants:
        if isinstance(el, NavigableString):
            text = el.strip()
            if not text or len(text) > 60:
                continue
            m = DATE_RE.match(text)
            if m:
                date = full_date(m.group(1), m.group(2), now)
                continue
            t = TIME_RE.match(text)
            if t and ev is not None and not ev["time"]:
                ev["time"] = f"{int(t.group(1)):02d}:{t.group(2)}"
        elif isinstance(el, Tag) and el.name == "a":
            href = el.get("href", "")
            p = PID_RE.search(href)
            if p and date:
                ev = {"pid": p.group(1), "title": el.get_text(" ", strip=True) or "Etendus",
                      "date": date, "time": "", "link": None}
                events.append(ev)
            elif "piletimaailm" in href and "performances" in href and ev is not None:
                ev["link"] = href.split("?")[0]

    return {f"{e['pid']}|{e['date']}|{e['time']}": e for e in events}


def send_notification(text, click_url=None, title="Draamateatri piletid"):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("NTFY_TOPIC puudub, sõnum oleks olnud:\n" + text)
        return
    chunks, buf = [], ""
    for block in text.split("\n\n"):
        if len((buf + block).encode("utf-8")) > 3500:
            chunks.append(buf)
            buf = ""
        buf += block + "\n\n"
    chunks.append(buf)
    for i, chunk in enumerate(chunks, 1):
        headers = {"Title": title + (f" ({i}/{len(chunks)})" if len(chunks) > 1 else ""),
                   "Tags": "performing_arts"}
        if click_url:
            headers["Click"] = click_url
        r = requests.post(f"https://ntfy.sh/{topic}", data=chunk.strip().encode("utf-8"),
                          headers=headers, timeout=30)
        r.raise_for_status()
        time.sleep(1)


def sort_key(e):
    return (datetime.strptime(e["date"], "%d.%m.%Y"), e["time"])


def fmt(e):
    when = f"{e['date']} kell {e['time']}" if e["time"] else e["date"]
    line = f"{e['title']}\n{when}"
    return line + (f"\nOsta: {e['link']}" if e["link"] else "\n(pileteid veel müügis pole)")


def main():
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    state = load_json(STATE_FILE, {})
    config = load_json(CONFIG_FILE, {})
    start_h, end_h = config.get("start_hour", 6), config.get("end_hour", 23)

    if os.getenv("GITHUB_EVENT_NAME") == "schedule" and not start_h <= now.hour < end_h:
        print(f"Väljaspool kontrolliaega (kell {now:%H:%M}), vahele jäetud")
        return

    events = {}
    for page in config.get("source_pages") or [DEFAULT_PAGE]:
        try:
            events.update(parse_schedule(requests.get(page, headers=HEADERS, timeout=30).text, now))
        except requests.RequestException as e:
            print(f"Ei saanud lugeda {page}: {e}")
            return
        time.sleep(1)

    if not events:
        print("Mängukavast ei leitud ühtegi etendust - seisu ei muudeta.")
        return

    only = [t.lower() for t in config.get("only_titles", [])]
    if only:
        events = {k: v for k, v in events.items() if any(t in v["title"].lower() for t in only)}

    productions = {}
    for e in sorted(events.values(), key=sort_key):
        productions.setdefault(e["pid"], e["title"])
    on_sale = [e for e in events.values() if e["link"]]
    print(f"Etendusi mängukavas: {len(events)}, lavastusi: {len(productions)}, müügis: {len(on_sale)}")
    for e in sorted(on_sale, key=sort_key):
        print(f"  MÜÜGIS: {e['date']} {e['time']} {e['title']} -> {e['link']}")

    # Demorežiim: saadab päris etenduse põhjal näidisteate, seisu ei muuda
    if os.getenv("DEMO", "").lower() == "true":
        sample = sorted(on_sale, key=sort_key)[:1]
        if sample:
            text = "🎟 VABANESID PILETID (oli välja müüdud):\n\n" + fmt(sample[0])
            send_notification(text, click_url=sample[0]["link"])
        else:
            send_notification("Demo: hetkel pole ühtegi etendust müügis, aga teavitus töötab!")
        print("Demoteade saadetud, seisu ei muudetud.")
        return

    notified = state.get("notified", {})

    def recently_notified(link):
        ts = notified.get(link)
        return ts and (now - datetime.fromisoformat(ts)).total_seconds() < RENOTIFY_HOURS * 3600

    first_ever = "events" not in state
    prev_events = state.get("events", {})
    prev_prods = state.get("productions", {})

    returned, new_sale, new_prods = [], [], []
    if not first_ever:
        for pid, title in productions.items():
            if pid not in prev_prods:
                first = min((e for e in events.values() if e["pid"] == pid), key=sort_key)
                new_prods.append(first)
        new_pids = {e["pid"] for e in new_prods}
        for key, e in events.items():
            if not e["link"] or e["pid"] in new_pids or recently_notified(e["link"]):
                continue
            old = prev_events.get(key)
            if old is None:
                new_sale.append(e)
            elif not old.get("link"):
                returned.append(e)

    parts = []
    if returned:
        parts.append("🎟 VABANESID PILETID (oli välja müüdud):")
        parts += [fmt(e) for e in sorted(returned, key=sort_key)]
    if new_sale:
        parts.append("🆕 UUED ETENDUSED MÜÜGIS:")
        parts += [fmt(e) for e in sorted(new_sale, key=sort_key)]
    if new_prods:
        parts.append("✨ UUS LAVASTUS MÄNGUKAVAS (esimene etendus):")
        parts += [fmt(e) for e in sorted(new_prods, key=sort_key)]

    if parts:
        links = [e["link"] for e in returned + new_sale + new_prods if e["link"]]
        send_notification("\n\n".join(parts), click_url=links[0] if links else DEFAULT_PAGE)
        for e in returned + new_sale + new_prods:
            if e["link"]:
                notified[e["link"]] = now.isoformat(timespec="seconds")
    elif first_ever:
        send_notification(
            f"Jälgimine käivitatud: mängukavas {len(productions)} lavastust ja {len(events)} "
            f"etendust, neist müügis {len(on_sale)}. Annan teada, kui midagi muutub."
        )
    elif state.get("last_confirm") != today and config.get("send_when_nothing_new"):
        send_notification(
            f"Tere hommikust! Kontroll töötab: müügis {len(on_sale)} etendust, "
            f"uusi muudatusi pole. Annan teada, kui midagi lisandub."
        )

    notified = {k: v for k, v in notified.items()
                if (now - datetime.fromisoformat(v)).total_seconds() < 2 * 86400}
    new_state = {"last_confirm": today, "productions": productions,
                 "events": events, "notified": notified}
    if new_state != state:
        save_json(STATE_FILE, new_state)
    print(f"Valmis. Vabanenud: {len(returned)}, uued müügis: {len(new_sale)}, uued lavastused: {len(new_prods)}")


if __name__ == "__main__":
    sys.exit(main())
