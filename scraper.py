"""
Draamateatri piletiteavitus.
Loeb Draamateatri mängukava, leiab etendused, mille juures on Piletimaailma
ostulink (st pileteid on saada), võrdleb eelmise seisuga ja saadab uued
saadavused telefoni (ntfy).
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
HEADERS = {"User-Agent": "Mozilla/5.0 (isiklik piletiteavitus, 1 paring paevas)"}
DEFAULT_PAGE = "https://www.draamateater.ee/mangukava/?tickets=available"

# Kuupäeva rida mängukavas, nt "Tu 17.11", "K 18.11" või lihtsalt "17.11"
DATE_RE = re.compile(r"^(?:[^\W\d]{1,3}\.?\s+)?(\d{2})\.(\d{2})(?![.\d])")
TIME_RE = re.compile(r"^(\d{1,2})[:.](\d{2})$")


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
    """Tagastab {ostulink: {"date", "time", "title"}} kõigi etenduste kohta,
    mille juures on Piletimaailma ostulink."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    items = {}
    date = title = clock = None
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
            if t:
                clock = f"{int(t.group(1)):02d}:{t.group(2)}"
        elif isinstance(el, Tag) and el.name == "a":
            href = el.get("href", "")
            if "lavastus/p/" in href:
                title = el.get_text(" ", strip=True) or title
            elif "piletimaailm" in href and "performances" in href and date:
                link = href.split("?")[0]
                items[link] = {"date": date, "time": clock or "", "title": title or "Etendus"}
    return items


def send_notification(text, click_url=None):
    """Saadab märguande telefoni ntfy äppi."""
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
        headers = {"Title": "Draamateatri piletid", "Tags": "performing_arts"}
        if len(chunks) > 1:
            headers["Title"] += f" ({i}/{len(chunks)})"
        if click_url:
            headers["Click"] = click_url
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=chunk.strip().encode("utf-8"),
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        time.sleep(1)


def sort_key(item):
    d = datetime.strptime(item["date"], "%d.%m.%Y")
    return (d, item["time"])


def main():
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    state = load_json(STATE_FILE, {})
    config = load_json(CONFIG_FILE, {})

    # Ajastatud käivitus: saada ainult kell 6-8 Tallinna aja järgi ja üks kord päevas
    if os.getenv("GITHUB_EVENT_NAME") == "schedule":
        if not 6 <= now.hour <= 8 or state.get("last_sent") == today:
            print(f"Vahele jäetud (kell {now:%H:%M}, viimati saadetud {state.get('last_sent')})")
            return

    current = {}
    for page in config.get("source_pages") or [DEFAULT_PAGE]:
        try:
            current.update(parse_schedule(requests.get(page, headers=HEADERS, timeout=30).text, now))
        except requests.RequestException as e:
            print(f"Ei saanud lugeda {page}: {e}")
            return  # seisu ei kirjutata üle, kui leht ei avanenud
        time.sleep(1)

    only = [t.lower() for t in config.get("only_titles", [])]
    if only:
        current = {k: v for k, v in current.items() if any(t in v["title"].lower() for t in only)}

    print(f"Müügis etendusi leitud: {len(current)}")
    for link, it in sorted(current.items(), key=lambda kv: sort_key(kv[1])):
        print(f"  {it['date']} {it['time']} {it['title']} -> {link}")

    prev = state.get("available", {})
    new = {k: v for k, v in current.items() if k not in prev}

    if new:
        ordered = sorted(new.items(), key=lambda kv: sort_key(kv[1]))
        parts = [f"Uued piletid müügis ({now:%d.%m}):"]
        for link, it in ordered:
            when = f"{it['date']} kell {it['time']}" if it["time"] else it["date"]
            parts.append(f"{it['title']}\n{when}\nOsta: {link}")
        send_notification("\n\n".join(parts), click_url=ordered[0][0])
    elif config.get("send_when_nothing_new"):
        send_notification(
            f"{now:%d.%m}: uusi vabu pileteid ei leitud. Müügis kokku {len(current)} etendust."
        )

    state = {
        "last_sent": today,
        "last_run": now.isoformat(timespec="seconds"),
        "available": current,
    }
    save_json(STATE_FILE, state)
    print(f"Valmis, uusi etendusi teavituses: {len(new)}")


if __name__ == "__main__":
    sys.exit(main())
