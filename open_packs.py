"""Ouvre automatiquement les paquets WikiMasters d'un compte.

Usage :
  python open_packs.py setup chemin/vers/wiki.har   # extrait une session du HAR dans session.json
  python open_packs.py                              # ouvre tous les paquets disponibles
  python open_packs.py test-discord                 # envoie un message de test sur les webhooks

Dans GitHub Actions, le workflow lance le script une fois par compte de accounts.json et
lui passe uniquement les secrets de ce compte (variables d'environnement ci-dessous).
Hors Actions, le script utilise session.json.

Supabase fait tourner le refresh token a chaque utilisation : le nouveau est
re-sauvegarde (fichier local, ou secret GitHub du compte si on tourne dans Actions).
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
from zoneinfo import ZoneInfo

SITE = "https://www.wiki-masters.com"
PROJECT_REF = "cyrxjeppjqsxxjayfrur"
SUPABASE = f"https://{PROJECT_REF}.supabase.co"
COOKIE_NAME = f"sb-{PROJECT_REF}-auth-token"
CHUNK_SIZE = 3180
MAX_PACKS_PER_RUN = 20
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0"

HERE = Path(__file__).resolve().parent
SESSION_FILE = HERE / "session.json"
IN_ACTIONS = bool(os.environ.get("GITHUB_ACTIONS"))

# Compte traite par ce run (fourni par le workflow, voir accounts.json).
ACCOUNT_NAME = os.environ.get("ACCOUNT_NAME") or "Moi"
ACCOUNT_SLUG = os.environ.get("ACCOUNT_SLUG") or "moi"
TOKEN_SECRET = os.environ.get("TOKEN_SECRET") or "WM_REFRESH_TOKEN"
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID") or ""
PUBLIC_WEBHOOK = os.environ.get("DISCORD_WEBHOOK") or ""
PRIVATE_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_PRIVATE") or ""
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN") or ""
PUBLIC_MIN_RARITY = os.environ.get("DISCORD_PUBLIC_MIN_RARITY") or "L"
PRIVATE_MIN_RARITY = os.environ.get("DISCORD_PRIVATE_MIN_RARITY") or "SR"

LOG_FILE = HERE / f"cards_log_{ACCOUNT_SLUG}.csv"
COLLECTION_FILE = HERE / f"collection_{ACCOUNT_SLUG}.csv"

# Ordre de secours si la base ne repond pas ; l'ordre reel est lu dans la table cards.
FALLBACK_RARITIES = ["C", "PC", "R", "SR", "UR", "L"]
RARITY_COLORS = {"R": 0x3B82F6, "SR": 0xA855F7, "UR": 0xF59E0B, "L": 0xEF4444}


def http(method, url, headers=None, body=None, retries=3):
    # Reessaie sur les erreurs reseau / 5xx (pannes passageres Cloudflare/Supabase).
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            try:
                status, payload = e.code, json.loads(text)
            except ValueError:
                status, payload = e.code, text[:500]
        except (urllib.error.URLError, TimeoutError) as e:
            status, payload = 0, str(e)
        if status and status < 500:
            return status, payload
        if attempt < retries - 1:
            time.sleep(10 * (attempt + 1))
    return status, payload


# --- Session -----------------------------------------------------------------

def setup(har_path):
    har = json.loads(Path(har_path).read_text(encoding="utf-8"))
    refresh_token = anon_key = None
    # N'importe quelle requete vers le site porte le cookie de session ; on garde la plus recente.
    for entry in har["log"]["entries"]:
        req = entry["request"]
        chunks = sorted((c for c in req.get("cookies", []) if c["name"].startswith(COOKIE_NAME)),
                        key=lambda c: c["name"])
        if "wiki-masters.com" in req["url"] and chunks:
            raw = "".join(c["value"] for c in chunks)
            if raw.startswith("base64-"):
                raw = raw[len("base64-"):]
                raw = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
            refresh_token = json.loads(raw)["refresh_token"]
        if SUPABASE in req["url"] and not anon_key:
            anon_key = next((h["value"] for h in req["headers"] if h["name"].lower() == "apikey"), None)
    if not refresh_token or not anon_key:
        sys.exit("Session introuvable dans le HAR (es-tu bien connecte et as-tu recharge la page ?)")
    save_session({"refresh_token": refresh_token, "anon_key": anon_key})
    print(f"OK : session enregistree dans {SESSION_FILE.name}")


def load_session():
    if os.environ.get("WM_REFRESH_TOKEN"):
        return {"refresh_token": os.environ["WM_REFRESH_TOKEN"], "anon_key": os.environ["WM_ANON_KEY"]}
    if IN_ACTIONS:
        fail(f"Le secret {TOKEN_SECRET} (ou WM_ANON_KEY) est vide ou manquant.")
    if SESSION_FILE.exists():
        return json.loads(SESSION_FILE.read_text())
    sys.exit("Pas de session : lance d'abord  python open_packs.py setup wiki.har")


def set_gh_secret(name, value):
    # Passe par stdin pour que la valeur n'apparaisse jamais dans une trace d'erreur.
    r = subprocess.run(["gh", "secret", "set", name], input=value, text=True, capture_output=True)
    if r.returncode != 0:
        fail(f"Impossible d'ecrire le secret {name} : {r.stderr.strip()}\n"
             "Verifie que GH_PAT a la permission Secrets: Read and write sur ce depot.")


def check_gh_secret_access():
    # Verifie les droits AVANT de consommer le refresh token (usage unique),
    # sinon un echec d'ecriture ferait perdre la session.
    if IN_ACTIONS:
        set_gh_secret("WM_WRITE_CHECK", "ok")


def save_session(sess):
    if IN_ACTIONS:
        # Masque le nouveau token dans les logs (le depot est public).
        print(f"::add-mask::{sess['refresh_token']}", flush=True)
        # Le refresh token est a usage unique : on met a jour le secret du compte pour le prochain run.
        set_gh_secret(TOKEN_SECRET, sess["refresh_token"])
    else:
        SESSION_FILE.write_text(json.dumps(sess))


def refresh(sess):
    status, data = http("POST", f"{SUPABASE}/auth/v1/token?grant_type=refresh_token",
                        {"apikey": sess["anon_key"], "Content-Type": "application/json"},
                        {"refresh_token": sess["refresh_token"]})
    if status == 0 or status >= 500:
        sys.exit(f"Serveur indisponible ({status}) : {data}\nLa session est intacte, le prochain run reessaiera.")
    if status != 200:
        fail(f"Session expiree ou revoquee ({status}) : {data}\n"
             "Reconnecte-toi sur le site (fenetre privee), refais l'etape setup "
             f"et mets a jour le secret {TOKEN_SECRET}.")
    sess["refresh_token"] = data["refresh_token"]
    save_session(sess)
    return data


def auth_cookie(session_data):
    keys = ("access_token", "token_type", "expires_in", "expires_at", "refresh_token", "user")
    payload = json.dumps({k: session_data.get(k) for k in keys}, separators=(",", ":"))
    value = "base64-" + base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    chunks = [value[i:i + CHUNK_SIZE] for i in range(0, len(value), CHUNK_SIZE)]
    return "; ".join(f"{COOKIE_NAME}.{i}={c}" for i, c in enumerate(chunks))


# --- Donnees ---------------------------------------------------------------------

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


def supabase_get_all(path, anon_key, access_token, page_size=1000):
    # Lecture paginee de l'API REST Supabase (limitee a 1000 lignes par requete).
    headers = {"apikey": anon_key, "Authorization": f"Bearer {access_token}"}
    rows, offset = [], 0
    while True:
        sep = "&" if "?" in path else "?"
        status, data = http("GET", f"{SUPABASE}/rest/v1/{path}{sep}limit={page_size}&offset={offset}", headers)
        if status != 200:
            return status, data
        rows += data
        if len(data) < page_size:
            return 200, rows
        offset += page_size


def rarity_orders(anon_key, access_token):
    # Correspondance rarete -> rarity_order lue dans la table cards.
    headers = {"apikey": anon_key, "Authorization": f"Bearer {access_token}"}
    orders = {}
    for order in range(10):
        status, data = http("GET", f"{SUPABASE}/rest/v1/cards?select=rarity&rarity_order=eq.{order}&limit=1",
                            headers)
        if status == 200 and data:
            orders[data[0]["rarity"]] = order
    if not orders:
        return {r: i for i, r in enumerate(FALLBACK_RARITIES)}
    print("Raretes : " + ", ".join(f"{r}={o}" for r, o in sorted(orders.items(), key=lambda x: x[1])))
    return orders


def export_collection(anon_key, session_data):
    # Exporte toute la collection du compte (y compris les cartes obtenues hors script).
    token, user_id = session_data["access_token"], session_data["user"]["id"]
    fields = "wikipedia_title,rarity,rarity_order,atk,def,wikipedia_url"
    status, rows = supabase_get_all(f"user_cards?select=card_id,is_shiny,cards({fields})"
                                    f"&user_id=eq.{user_id}&order=id", anon_key, token)
    if status != 200:
        print(f"Export de la collection impossible ({status}) : {rows}")
        return None
    cards = []
    for r in rows:
        c = r.get("cards") or {}
        cards.append({"titre": c.get("wikipedia_title"), "rarete": c.get("rarity"),
                      "rarity_order": c.get("rarity_order") or 0, "shiny": r.get("is_shiny"),
                      "atk": c.get("atk"), "def": c.get("def"), "url": c.get("wikipedia_url")})
    cards.sort(key=lambda c: (-c["rarity_order"], c["titre"] or ""))
    with COLLECTION_FILE.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["rarete", "titre", "shiny", "atk", "def", "url"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(cards)
    counts = {}
    for c in cards:
        counts[c["rarete"]] = counts.get(c["rarete"], 0) + 1
    detail = ", ".join(f"{n} {r}" for r, n in sorted(counts.items(), key=lambda x: -x[1]))
    line = f"Collection : {len(cards)} cartes ({detail})."
    print(line)
    return line


def write_summary(opened_cards, status_line):
    # Tableau affiche sur la page du run GitHub Actions.
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [f"### {ACCOUNT_NAME} : {len(opened_cards)} carte(s) obtenue(s)", "", status_line, ""]
    if opened_cards:
        lines += ["| Paquet | Rarete | Carte | ATK | DEF |", "|---|---|---|---|---|"]
        lines += [f"| {n} | {c.get('rarity')} | [{c.get('wikipedia_title')}]({c.get('wikipedia_url')}) "
                  f"| {c.get('atk')} | {c.get('def')} |" for n, c in opened_cards]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# --- Discord ---------------------------------------------------------------------

def discord_post(url, payload):
    if not url:
        return
    # Cloudflare bloque le User-Agent par defaut de urllib.
    status, data = http("POST", url, {"Content-Type": "application/json", "User-Agent": UA}, payload)
    if status not in (200, 204):
        print(f"Notification Discord echouee ({status}) : {data}")


def bot_dm(payload):
    # Envoie un message prive via le bot Discord ; renvoie False si impossible.
    if not (BOT_TOKEN and DISCORD_USER_ID):
        return False
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json",
               "User-Agent": "DiscordBot (https://github.com/Josue-Baquero/WikiMasters, 1.0)"}
    status, channel = http("POST", "https://discord.com/api/v10/users/@me/channels", headers,
                           {"recipient_id": DISCORD_USER_ID})
    if status == 200:
        status, data = http("POST", f"https://discord.com/api/v10/channels/{channel['id']}/messages",
                            headers, payload)
        if status == 200:
            return True
    print(f"DM Discord echoue ({status}) : bot absent du serveur ou DM bloques ?")
    return False


def private_post(payload):
    # Notification privee : DM par le bot si configure, sinon webhook du salon prive.
    if not bot_dm(payload):
        discord_post(PRIVATE_WEBHOOK, payload)


def private_channel_configured():
    return bool((BOT_TOKEN and DISCORD_USER_ID) or PRIVATE_WEBHOOK)


def card_embed(c):
    embed = {"title": f"[{c.get('rarity')}] {c.get('wikipedia_title')}", "url": c.get("wikipedia_url"),
             "description": (c.get("summary") or "")[:300],
             "color": RARITY_COLORS.get(c.get("rarity"), 0x10B981),
             "fields": [{"name": "ATK", "value": str(c.get("atk")), "inline": True},
                        {"name": "DEF", "value": str(c.get("def")), "inline": True}]}
    embed = {k: v for k, v in embed.items() if v}  # Discord refuse les champs vides
    if c.get("image_url") and not c.get("hide_image") and not c.get("nsfw_image"):
        embed["thumbnail"] = {"url": c["image_url"]}
    return embed


def cards_at_least(cards, min_rarity, orders):
    min_order = orders.get(min_rarity, max(orders.values(), default=0) + 1)
    rares = [c for c in cards if (c.get("rarity_order") or 0) >= min_order]
    return sorted(rares, key=lambda c: -(c.get("rarity_order") or 0))


def notify_cards(cards, collection_line, orders):
    # Salon prive du compte : SR et plus par defaut.
    rares = cards_at_least(cards, PRIVATE_MIN_RARITY, orders)
    if rares:
        content = f"🎴 **{ACCOUNT_NAME} : {len(rares)} carte(s) {PRIVATE_MIN_RARITY}+ obtenue(s) !**"
        if collection_line:
            content += f"\n{collection_line}"
        private_post({"content": content, "embeds": [card_embed(c) for c in rares[:10]]})
    # Salon public commun : L seulement par defaut.
    rares = cards_at_least(cards, PUBLIC_MIN_RARITY, orders)
    if rares:
        who = f"<@{DISCORD_USER_ID}>" if DISCORD_USER_ID else f"**{ACCOUNT_NAME}**"
        discord_post(PUBLIC_WEBHOOK, {"content": f"🌟 {who} a tire {len(rares)} carte(s) {PUBLIC_MIN_RARITY}+ !",
                                      "embeds": [card_embed(c) for c in rares[:10]]})


def fail(message):
    # Erreur qui demande une action manuelle : alerte privee (ou salon public a defaut).
    payload = {"content": f"⚠️ **Le script WikiMasters est bloque pour {ACCOUNT_NAME}**, "
                          f"action requise :\n```{message}```"}
    if private_channel_configured():
        private_post(payload)
    else:
        discord_post(PUBLIC_WEBHOOK, payload)
    sys.exit(message)


# --- Execution -------------------------------------------------------------------

def open_packs():
    sess = load_session()
    check_gh_secret_access()
    session_data = refresh(sess)
    orders = rarity_orders(sess["anon_key"], session_data["access_token"])
    headers = {"Cookie": auth_cookie(session_data), "User-Agent": UA, "Origin": SITE,
               "Referer": f"{SITE}/pulls", "Accept": "*/*"}
    opened, opened_cards, status_line = 0, [], ""
    for _ in range(MAX_PACKS_PER_RUN):
        status, data = http("POST", f"{SITE}/api/packs/open", headers)
        if isinstance(data, dict) and "cards" not in data and data.get("next_regen_at"):
            next_regen = datetime.fromisoformat(data["next_regen_at"].replace("Z", "+00:00"))
            status_line = (f"Plus de paquets. Prochain paquet a "
                           f"{next_regen.astimezone(ZoneInfo('Europe/Paris')):%H:%M} (heure de Paris).")
            print(status_line)
            break
        if status != 200 or not isinstance(data, dict) or "cards" not in data:
            status_line = f"Arret : {status} {data}"
            print(status_line)
            break
        opened += 1
        cards = data["cards"]
        log_cards(cards)
        opened_cards += [(opened, c) for c in cards]
        print(f"Paquet {opened} : " + ", ".join(f"[{c['rarity']}] {c['wikipedia_title']}" for c in cards))
        if data.get("packs_remaining", 0) <= 0:
            status_line = "Tous les paquets disponibles ont ete ouverts."
            break
        time.sleep(1.5)
    print(f"{opened} paquet(s) ouvert(s).")
    collection_line = export_collection(sess["anon_key"], session_data)
    if collection_line:
        status_line += "\n\n" + collection_line
    write_summary(opened_cards, status_line)
    notify_cards([c for _, c in opened_cards], collection_line, orders)


def test_discord():
    private_post({"content": f"✅ Test : notifications privees de {ACCOUNT_NAME} "
                             f"(cartes {PRIVATE_MIN_RARITY}+ et alertes)."})
    discord_post(PUBLIC_WEBHOOK, {"content": f"✅ Test : salon public, cartes {PUBLIC_MIN_RARITY}+ "
                                             f"(envoye par le compte {ACCOUNT_NAME})."})
    mode = "DM bot" if BOT_TOKEN and DISCORD_USER_ID else ("webhook prive" if PRIVATE_WEBHOOK else "aucun")
    print(f"{ACCOUNT_NAME} : notifications privees = {mode}, "
          f"webhook public {'OK' if PUBLIC_WEBHOOK else 'non configure'}.")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "setup":
        setup(sys.argv[2])
    elif len(sys.argv) >= 2 and sys.argv[1] == "test-discord":
        test_discord()
    else:
        open_packs()
