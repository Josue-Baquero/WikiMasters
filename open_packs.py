"""Ouvre automatiquement les paquets WikiMasters.

Usage :
  python open_packs.py setup chemin/vers/wiki.har   # une seule fois : extrait la session du HAR
  python open_packs.py                              # ouvre tous les paquets disponibles

La session (refresh token) est stockee dans session.json, ou lue depuis les
variables d'environnement WM_REFRESH_TOKEN / WM_ANON_KEY (GitHub Actions).
Supabase fait tourner le refresh token a chaque utilisation : le nouveau est
re-sauvegarde (fichier local, ou secret GitHub si on tourne dans Actions).
"""
import base64
import csv
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SITE = "https://www.wiki-masters.com"
PROJECT_REF = "cyrxjeppjqsxxjayfrur"
SUPABASE = f"https://{PROJECT_REF}.supabase.co"
COOKIE_NAME = f"sb-{PROJECT_REF}-auth-token"
CHUNK_SIZE = 3180
MAX_PACKS_PER_RUN = 20
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0"

HERE = Path(__file__).resolve().parent
SESSION_FILE = HERE / "session.json"
LOG_FILE = HERE / "cards_log.csv"


def http(method, url, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(text)
        except ValueError:
            return e.code, text[:500]


def setup(har_path):
    har = json.loads(Path(har_path).read_text(encoding="utf-8"))
    refresh_token = anon_key = None
    for entry in har["log"]["entries"]:
        req = entry["request"]
        if "/api/packs/open" in req["url"] and not refresh_token:
            chunks = sorted((c for c in req.get("cookies", []) if c["name"].startswith(COOKIE_NAME)),
                            key=lambda c: c["name"])
            raw = "".join(c["value"] for c in chunks)
            if raw.startswith("base64-"):
                raw = raw[len("base64-"):]
                raw = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
            refresh_token = json.loads(raw)["refresh_token"]
        if SUPABASE in req["url"] and not anon_key:
            anon_key = next((h["value"] for h in req["headers"] if h["name"].lower() == "apikey"), None)
    if not refresh_token or not anon_key:
        sys.exit("Session introuvable dans le HAR (as-tu ouvert un paquet pendant l'enregistrement ?)")
    save_session({"refresh_token": refresh_token, "anon_key": anon_key})
    print(f"OK : session enregistree dans {SESSION_FILE.name}")
    print("Pour GitHub Actions, copie ces deux valeurs dans les secrets WM_REFRESH_TOKEN et WM_ANON_KEY.")


def load_session():
    if os.environ.get("WM_REFRESH_TOKEN"):
        return {"refresh_token": os.environ["WM_REFRESH_TOKEN"], "anon_key": os.environ["WM_ANON_KEY"]}
    if SESSION_FILE.exists():
        return json.loads(SESSION_FILE.read_text())
    sys.exit("Pas de session : lance d'abord  python open_packs.py setup wiki.har")


def save_session(sess):
    if os.environ.get("GITHUB_ACTIONS"):
        # Le refresh token est a usage unique : on met a jour le secret pour le prochain run.
        subprocess.run(["gh", "secret", "set", "WM_REFRESH_TOKEN", "--body", sess["refresh_token"]],
                       check=True, capture_output=True)
    else:
        SESSION_FILE.write_text(json.dumps(sess))


def refresh(sess):
    status, data = http("POST", f"{SUPABASE}/auth/v1/token?grant_type=refresh_token",
                        {"apikey": sess["anon_key"], "Content-Type": "application/json"},
                        {"refresh_token": sess["refresh_token"]})
    if status != 200:
        sys.exit(f"Echec du rafraichissement de session ({status}) : {data}\n"
                 "Reconnecte-toi sur le site et refais l'etape setup.")
    sess["refresh_token"] = data["refresh_token"]
    save_session(sess)
    return data


def auth_cookie(session_data):
    keys = ("access_token", "token_type", "expires_in", "expires_at", "refresh_token", "user")
    payload = json.dumps({k: session_data.get(k) for k in keys}, separators=(",", ":"))
    value = "base64-" + base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    chunks = [value[i:i + CHUNK_SIZE] for i in range(0, len(value), CHUNK_SIZE)]
    return "; ".join(f"{COOKIE_NAME}.{i}={c}" for i, c in enumerate(chunks))


def log_cards(cards):
    new = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "rarete", "titre", "atk", "def", "url"])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for c in cards:
            w.writerow([now, c.get("rarity"), c.get("wikipedia_title"), c.get("atk"), c.get("def"),
                        c.get("wikipedia_url")])


def open_packs():
    sess = load_session()
    cookie = auth_cookie(refresh(sess))
    headers = {"Cookie": cookie, "User-Agent": UA, "Origin": SITE, "Referer": f"{SITE}/pulls",
               "Accept": "*/*"}
    opened = 0
    for _ in range(MAX_PACKS_PER_RUN):
        status, data = http("POST", f"{SITE}/api/packs/open", headers)
        if isinstance(data, dict) and "cards" not in data and data.get("next_regen_at"):
            next_regen = datetime.fromisoformat(data["next_regen_at"].replace("Z", "+00:00"))
            print(f"Plus de paquets. Prochain paquet a {next_regen.astimezone():%H:%M} (heure locale).")
            break
        if status != 200 or not isinstance(data, dict) or "cards" not in data:
            print(f"Arret : {status} {data}")
            break
        opened += 1
        cards = data["cards"]
        log_cards(cards)
        print(f"Paquet {opened} : " + ", ".join(f"[{c['rarity']}] {c['wikipedia_title']}" for c in cards))
        if data.get("packs_remaining", 0) <= 0:
            break
        time.sleep(1.5)
    print(f"{opened} paquet(s) ouvert(s).")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "setup":
        setup(sys.argv[2])
    else:
        open_packs()
