"""
Draamateatri piletiteavitus.
Käib läbi Piletimaailma lavastuste lehed, leiab etendused, kus on vabu kohti,
võrdleb eelmise päeva seisuga ja saadab uued saadavused WhatsAppi (CallMeBot).
"""
import json
import os
import re
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = "https://www.piletimaailm.com"
STATE_FILE = "state.json"
CONFIG_FILE = "config.json"
TZ = ZoneInfo("Europe/Tallinn")
HEADERS = {"User-Agent": "Mozilla/5.0 (isiklik piletiteavitus, 1 paring paevas)"}

DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})")
SEATS_RE = re.compile(r"Available seats:\s*(\d+)")
PERF_LINK_RE = re.compile(r"piletimaailm\.com/(?:[a-z]{2}/)?performances/(\d+)")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def discover_performance_ids(source_pages):
    """Otsib antud lehtedelt (nt Draamateatri mängukava) kõik Piletimaailma lavastuse lingid."""
    ids = set()
    for page in source_pages:
        try:
            ids.update(PERF_LINK_RE.findall(get(page)))
        except requests.RequestException as e:
            print(f"Ei saanud lugeda {page}: {e}")
        time.sleep(1)
    return ids


def parse_performance(pid):
    """Tagastab (pealkiri, {"PP.KK.AAAA HH:MM": vabu_kohti})."""
    html = get(f"{BASE}/performances/{pid}?lang=en_US")
    soup = BeautifulSoup(html, "html.parser")

    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(strip=True)
    elif soup.title:
        title = soup.title.get_text(strip=True)
    else:
        title = f"Lavastus {pid}"

    lines = [l.strip() for l in soup.get_text("\n").splitlines() if l.strip()]
    shows, current = {}, None
    for line in lines:
        d = DATE_RE.search(line)
        if d and len(line) < 30:  # kuupäeva rida, nt "Wed 19.11.2025 19:00"
            current = d.group(1)
            continue
        s = SEATS_RE.search(line)
        if s and current:
            shows[current] = int(s.group(1))
            current = None
    return title, shows


def send_whatsapp(text):
    phone = os.environ.get("WHATSAPP_PHONE")
    apikey = os.environ.get("CALLMEBOT_APIKEY")
    if not phone or not apikey:
        print("WhatsAppi andmed puuduvad, sõnum oleks olnud:\n" + text)
        return
    # Pikad sõnumid jagatakse osadeks
    chunks, buf = [], ""
    for block in text.split("\n\n"):
        if len(buf) + len(block) > 1500:
            chunks.append(buf)
            buf = ""
        buf += block + "\n\n"
    chunks.append(buf)
    for chunk in chunks:
        r = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": phone, "text": chunk.strip(), "apikey": apikey},
            timeout=30,
        )
        r.raise_for_status()
        time.sleep(3)


def main():
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    state = load_json(STATE_FILE, {"last_sent": None, "performances": {}})
    config = load_json(CONFIG_FILE, {})

    # Ajastatud käivitus: saada ainult kell 6-8 Tallinna aja järgi ja üks kord päevas
    if os.getenv("GITHUB_EVENT_NAME") == "schedule":
        if not 6 <= now.hour <= 8 or state.get("last_sent") == today:
            print(f"Vahele jäetud (kell {now:%H:%M}, viimati saadetud {state.get('last_sent')})")
            return

    ids = set(str(i) for i in config.get("performance_ids", []))
    ids |= discover_performance_ids(config.get("source_pages", []))
    only = [t.lower() for t in config.get("only_titles", [])]
    print(f"Kontrollin {len(ids)} lavastust")

    new_items = []
    for pid in sorted(ids):
        try:
            title, shows = parse_performance(pid)
        except requests.RequestException as e:
            print(f"Viga lavastusega {pid}: {e}")
            continue
        time.sleep(1)

        if only and not any(t in title.lower() for t in only):
            continue

        # Jäta minevikus olevad etendused välja
        shows = {
            d: s for d, s in shows.items()
            if datetime.strptime(d, "%d.%m.%Y %H:%M").replace(tzinfo=TZ) > now
        }
        prev = state["performances"].get(pid, {}).get("shows", {})
        fresh = [
            (d, s) for d, s in shows.items()
            if s > 0 and (d not in prev or prev[d] == 0)
        ]
        if fresh:
            fresh.sort(key=lambda x: datetime.strptime(x[0], "%d.%m.%Y %H:%M"))
            new_items.append((title, pid, fresh))
        state["performances"][pid] = {"title": title, "shows": shows}

    if new_items:
        parts = [f"🎭 Draamateatri uued piletid ({now:%d.%m})"]
        for title, pid, fresh in new_items:
            rows = "\n".join(f"• {d} – {s} vaba kohta" for d, s in fresh)
            parts.append(f"*{title}*\n{rows}\nOsta: {BASE}/performances/{pid}")
        send_whatsapp("\n\n".join(parts))
    elif config.get("send_when_nothing_new"):
        send_whatsapp(f"🎭 {now:%d.%m}: Draamateatris uusi vabu pileteid ei leitud.")

    state["last_sent"] = today
    state["last_run"] = now.isoformat(timespec="seconds")
    save_json(STATE_FILE, state)
    print(f"Valmis, uusi lavastusi teavituses: {len(new_items)}")


if __name__ == "__main__":
    sys.exit(main())
