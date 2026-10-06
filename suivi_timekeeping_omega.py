#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""suivi_timekeeping_omega.py

Exécution UNIQUE (pas de boucle interne) : relève TOUTES les montres Omega disponibles sur 
https://timekeeping.fr (Speedmaster, Seamaster, Constellation, De Ville, etc.) et ajoute 
une ligne par montre dans un CSV horodaté, uniquement si le prix a changé depuis le dernier relevé.

À chaque exécution, le script :
  1. met à jour le CSV (nouveautés, changements de prix, ventes) ;
  2. réécrit la page HTML omega_timekeeping_disponibles.html, qui liste toutes
     les montres actuellement en ligne sur le site, avec de vrais liens
     cliquables (à ouvrir dans un navigateur) ;
  3. envoie un message Telegram s'il y a une nouvelle offre, une vente ou un
     changement de prix (même bot et même fichier que recherche_immobilier.py
     et emails_scan.py : ~/.telegram_config, section [telegram], clés
     token_groq et chat_id). Si le fichier ou la section est absent, la
     notification est ignorée (avertissement sur stderr).

Ce script remplace suivi_timekeeping_speedmaster.py, qui se limitait à un seul modèle.

La récurrence quotidienne est assurée par cron, PAS par ce script :
    0 9 * * * suivi Omega timekeeping.fr

Stratégie (dans cet ordre) :
1. API Store WooCommerce (/wp-json/wc/store/v1/products) — publique, sans
   authentification, retourne du JSON propre par catégorie. C'est la voie normale sur un site WooCommerce. 
   On filtre sur la catégorie "omega".
2. Repli : si l'API est désactivée/absente (404/403), on récupère les URLs de fiches produits 
   via la recherche WordPress native (?s=omega, HTML côté serveur, pas de JS)
   puis on lit les balises meta (product:price:amount, product:availability) de chaque fiche.

NOTE IMPORTANTE : la page de listing https://timekeeping.fr/collections/omega
charge sa grille de produits en JavaScript après le chargement initial — 
elle est donc volontairement ignorée ici, un scraping HTML statique n'y verrait aucun produit.

NOTE SUR LES COLONNES DE RÉFÉRENCE (CSV) :
Le site expose deux informations bien distinctes qu'il ne faut pas confondre :
- "sku_woocommerce" : l'identifiant interne renvoyé par le site (SKU WooCommerce, 
  ou à défaut l'ID produit en base, ou en mode de repli le slug de l'URL). 
  C'est un identifiant de GESTION côté vendeur, saisi/généré indépendamment du titre,
  pas fiable comme référence horlogère : il peut être vide, réutilisé d'une ancienne fiche,
  ou totalement arbitraire. On le garde uniquement pour détecter de façon stable les 
  changements de prix d'une même fiche d'un relevé à l'autre.
- "ref." : la référence telle qu'annoncée dans le TITRE du produit (ex. "ref 2849"), 
  extraite par regex. C'est ce que voit vraiment le client sur le site ; 
  mais elle peut être absente (titre sans "ref").

Le format du CSV n'a PAS changé : l'URL de chaque fiche n'est utilisée que
pour la page HTML et les messages Telegram, elle n'est pas écrite dans le CSV.

Fichiers :
    omega_timekeeping.csv                    journal (ajout seul)
    omega_timekeeping_disponibles.html       montres actuellement en ligne,
                                             réécrit à chaque exécution

Dépendances : requests
    pip install requests --break-system-packages
"""

import configparser
import csv
import datetime
import html
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import requests

BASE = "https://timekeeping.fr"
STORE_API = f"{BASE}/wp-json/wc/store/v1/products"
SEARCH_URL = f"{BASE}/"
HERE = Path(__file__).resolve().parent
CSV_FILE = HERE / "omega_timekeeping.csv"
HTML_FILE = HERE / "omega_timekeeping_disponibles.html"
TELEGRAM_CONFIG = "~/.telegram_config"   # même fichier que recherche_immobilier.py
TELEGRAM_MAX_CHARS = 4000                # limite Telegram : 4096
USER_AGENT = "Mozilla/5.0 (compatible; JFBBot/1.0; +https://jfbconseil14.com)"
TIMEOUT = 30

META_PRICE_RE = re.compile(r'meta-product:price:amount:\s*([\d.]+)', re.I)
META_AVAIL_RE = re.compile(r'meta-product:availability:\s*(.+)', re.I)
META_TITLE_RE = re.compile(r'^title:\s*(.+)', re.I | re.M)
PRODUCT_URL_RE = re.compile(r'https://timekeeping\.fr/products/[a-z0-9\-]+', re.I)

# Référence "annoncée" : on cherche d'abord un motif "ref XXX" explicite dans le titre 
# (cas le plus fréquent et le plus fiable) ; à défaut, on retombe sur un éventuel nombre
# de référence en fin de titre (ex. "Chronomètre 2367").
REF_KEYWORD_RE = re.compile(r'\bref\.?\s*([A-Za-z0-9][\w./-]*)', re.IGNORECASE)
REF_TRAILING_RE = re.compile(r'(\d[\d.\-]*\d|\d)\s*$')


def extract_reference_from_title(title: str) -> str:
    """Extrait la référence annoncée par le vendeur depuis le titre du produit
    (et non depuis le SKU/ID interne, qui n'a souvent aucun rapport avec elle 
    — voir la note en tête de fichier)."""
    m = REF_KEYWORD_RE.search(title)
    if m:
        return m.group(1).rstrip('.,;:')
    m = REF_TRAILING_RE.search(title)
    if m:
        return m.group(1)
    return "N/A"


def _headers():
    return {"User-Agent": USER_AGENT}


def fetch_via_store_api() -> list[dict] | None:
    """Tente l'API Store WooCommerce. Retourne None si indisponible
    (l'appelant doit alors utiliser le repli), ou la liste de toutes les montres Omega."""
    try:
        resp = requests.get(
            STORE_API,
            params={"category": "omega", "per_page": 100},
            headers=_headers(),
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None

    rows = []
    for item in data:
        name = item.get("name", "N/A")
        prices = item.get("prices", {})
        minor_unit = int(prices.get("currency_minor_unit", 2))
        raw_price = prices.get("price")
        price = None
        if raw_price is not None:
            price = str(int(raw_price) / (10 ** minor_unit))
        stock = item.get("stock_availability", {}).get("text")
        if not stock:
            stock = "En stock" if item.get("is_in_stock") else "Rupture de stock"
        rows.append({
            "title": name,
            "sku_woocommerce": str(item.get("sku") or item.get("id", "N/A")),
            "ref.": extract_reference_from_title(name),
            "price": price or "N/A",
            "stock": stock,
            "url": item.get("permalink") or "",
        })
    return rows


def fetch_via_search_fallback() -> list[dict]:
    """Repli : recherche WordPress native (HTML statique) pour trouver les
    URLs de fiches Omega, puis lit les balises meta de chaque fiche."""
    resp = requests.get(
        SEARCH_URL, params={"s": "omega"}, headers=_headers(), timeout=TIMEOUT
    )
    resp.raise_for_status()
    urls = sorted(set(PRODUCT_URL_RE.findall(resp.text)))

    rows = []
    for url in urls:
        try:
            page = requests.get(url, headers=_headers(), timeout=TIMEOUT)
            page.raise_for_status()
        except requests.RequestException:
            continue
        title_m = META_TITLE_RE.search(page.text)
        title = title_m.group(1).strip() if title_m else url
        if "omega" not in title.lower():
            continue  # la recherche WordPress peut remonter du hors-sujet
        price_m = META_PRICE_RE.search(page.text)
        avail_m = META_AVAIL_RE.search(page.text)
        rows.append({
            "title": title,
            # Pas de vrai SKU disponible en mode de repli (pas d'appel à l'API Store) : 
            # on utilise le slug de l'URL comme identifiant interne stable pour le suivi des prix.
            "sku_woocommerce": url.rsplit("/", 1)[-1],
            "ref.": extract_reference_from_title(title),
            "price": price_m.group(1) if price_m else "N/A",
            "stock": avail_m.group(1).strip() if avail_m else "N/A",
            "url": url,
        })
    return rows


SOLD_STOCK_LABEL = "Vendu (retiré du site)"


def _last_known_state(csv_path: Path) -> dict[str, dict]:
    """Retourne, pour chaque sku_woocommerce (identifiant interne de fiche,
    seul champ stable pour le suivi — voir note en tête de fichier), les infos
    (titre, référence annoncée, prix, stock) de sa toute dernière ligne connue
    dans le CSV (le fichier est append-only et trié chronologiquement, donc 
    la dernière occurrence = l'état le plus récent connu pour cette fiche)."""
    if not csv_path.exists():
        return {}
    last: dict[str, dict] = {}
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            last[row["sku_woocommerce"]] = {
                "title": row["title"],
                "ref.": row.get("ref.", "N/A"),
                "price": row["price_eur"],
                "stock": row["stock"],
            }
    return last


def write(csv_path: Path, data: list[dict]) -> dict:
    """Ajoute une ligne par montre dont le prix a changé depuis le dernier
    relevé pour cette fiche (identifiée par sku_woocommerce), PLUS une ligne
    pour chaque fiche précédemment connue qui a disparu du relevé du jour
    (vente probable), tant qu'elle n'a pas déjà été marquée comme vendue auparavant.

    Une fiche précédemment marquée vendue qui réapparaît sur le site est
    traitée comme une nouvelle offre.

    Retourne un dictionnaire :
        "first_run" : True si le CSV n'existait pas ou était vide (aucun historique)
        "new"       : fiches jamais vues (ou réapparues après une vente)
        "price"     : [(fiche, ancien_prix)] pour les changements de prix
        "sold"      : fiches disparues du site depuis le dernier relevé
    """
    file_exists = csv_path.exists()
    previous = _last_known_state(csv_path)
    now = datetime.datetime.now().isoformat(sep=" ", timespec="seconds")

    new_rows: list[dict] = []
    price_rows: list[tuple[dict, str]] = []
    for row in data:
        old = previous.get(row["sku_woocommerce"])
        if old is None or old["stock"] == SOLD_STOCK_LABEL:
            new_rows.append(row)
        elif old["price"] != row["price"]:
            price_rows.append((row, old["price"]))

    current_skus = {row["sku_woocommerce"] for row in data}
    sold_rows = []
    for sku, info in previous.items():
        if sku in current_skus:
            continue  # toujours présente sur le site
        if info["stock"] == SOLD_STOCK_LABEL:
            continue  # déjà signalée vendue lors d'un run précédent
        sold_rows.append({
            "title": info["title"],
            "sku_woocommerce": sku,
            "ref.": info["ref."],
            "price": info["price"],
            "stock": SOLD_STOCK_LABEL,
        })

    result = {"first_run": not previous, "new": new_rows,
              "price": price_rows, "sold": sold_rows}

    to_write = new_rows + [r for r, _ in price_rows] + sold_rows
    if not to_write:
        return result

    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["datetime", "title", "sku_woocommerce",
                              "ref.", "price_eur", "stock"])
        for row in to_write:
            writer.writerow([now, row["title"], row["sku_woocommerce"],
                              row["ref."], row["price"], row["stock"]])
    return result


def migrate_legacy_csv(csv_path: Path) -> None:
    """Migre une seule fois un CSV au format
    (datetime,title,sku_woocommerce,ref.,price_eur,stock).
    La colonne "reference" historique devient "sku_woocommerce" telle quelle
    (elle contenait déjà le SKU/ID interne, pas la référence horlogère) ; 
    "ref." est reconstituée à partir du titre.
    Ne fait rien si le fichier n'existe pas ou est déjà au nouveau format."""
    if not csv_path.exists():
        return
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if "ref." in fieldnames or "reference" not in fieldnames:
            return  # déjà migré, ou format inconnu : on ne touche à rien
        rows = list(reader)

    for row in rows:
        row["sku_woocommerce"] = row.pop("reference")
        row["ref."] = extract_reference_from_title(row["title"])

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f, fieldnames=["datetime", "title", "sku_woocommerce",
                           "ref.", "price_eur", "stock"]
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"[INFO] {csv_path} migré vers le nouveau format "
          f"({len(rows)} ligne(s)).")

# --------------------------------------------------------------------------
# Page HTML des montres actuellement en ligne
# --------------------------------------------------------------------------

def fmt_price(raw: str) -> str:
    """'4500.0' -> '4 500 €' ; 'N/A' reste 'N/A'."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return str(raw)
    text = f"{value:,.2f}".replace(",", " ").rstrip("0").rstrip(".")
    return f"{text} €"


def write_html_page(data: list[dict], changes: dict) -> None:
    """Page HTML listant toutes les montres actuellement présentes sur le site, 
    avec de VRAIS liens cliquables. Réécrite à chaque exécution (état du jour, pas un journal). 
    Les montres nouvelles ou dont le prix vient de changer lors de ce relevé sont signalées par une pastille."""
    def esc(v):
        return html.escape(str(v or ""))

    new_skus = {r["sku_woocommerce"] for r in changes["new"]}
    old_prices = {r["sku_woocommerce"]: old for r, old in changes["price"]}

    def sort_key(r):
        try:
            return float(r["price"])
        except (TypeError, ValueError):
            return float("inf")

    items = sorted(data, key=sort_key)
    rows_html = []
    for r in items:
        sku = r["sku_woocommerce"]
        price = esc(fmt_price(r["price"]))
        badge = ""
        if sku in new_skus and not changes["first_run"]:
            badge = ' <span class="nouveau">Nouveau</span>'
        elif sku in old_prices:
            price += (f' <span class="ancien">(auparavant '
                      f'{esc(fmt_price(old_prices[sku]))})</span>')
        link = (f'<a href="{esc(r.get("url"))}" target="_blank" '
                f'rel="noopener">Voir la fiche</a>') if r.get("url") else ""
        rows_html.append(
            "<tr>"
            f"<td>{esc(r['title'])}{badge}</td>"
            f"<td>{esc(r['ref.'])}</td>"
            f"<td>{price}</td>"
            f"<td>{esc(r['stock'])}</td>"
            f"<td>{link}</td>"
            "</tr>")

    generated = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
    body = "\n".join(rows_html) if rows_html else \
        '<tr><td colspan="5">Aucune montre pour le moment.</td></tr>'
    html_doc = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Omega — timekeeping.fr</title>
<style>
 body {{ font-family: sans-serif; margin: 2em; }}
 table {{ border-collapse: collapse; width: 100%; }}
 th, td {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
 th {{ background: #f0f0f0; }}
 tr:nth-child(even) {{ background: #fafafa; }}
 .ancien {{ color: #b00; font-size: 0.9em; }}
 .nouveau {{ background: #2e7d32; color: #fff; border-radius: 4px;
            padding: 1px 6px; font-size: 0.8em; margin-left: 6px; }}
 caption {{ text-align: left; margin-bottom: 0.5em; color: #555; }}
</style></head>
<body>
<h1>Montres Omega en ligne sur Timekeeping.fr</h1>
<table>
<caption>{len(items)} montre(s) — relevé du {generated} — triées par prix croissant</caption>
<tr><th>Montre</th><th>Réf.</th><th>Prix</th><th>Stock</th><th></th></tr>
{body}
</table>
</body></html>
"""
    tmp = HTML_FILE.with_suffix(".html.tmp")
    tmp.write_text(html_doc, encoding="utf-8")
    tmp.replace(HTML_FILE)


# --------------------------------------------------------------------------
# Notification Telegram
# --------------------------------------------------------------------------

def load_telegram_config(path: Path):
    """Même fichier/bot que emails_scan.py et recherche_immobilier.py
    (section [telegram], clés token_groq et chat_id)."""
    parser = configparser.ConfigParser()
    parser.read(path)
    if not parser.has_section("telegram"):
        return None, None
    return (parser.get("telegram", "token_groq", fallback=None),
            parser.get("telegram", "chat_id", fallback=None))


def send_telegram(token: str | None, chat_id: str | None, text: str) -> None:
    """Échoue silencieusement (avertissement sur stderr) : une notification
    manquée ne doit jamais empêcher le reste du script de s'exécuter."""
    if not token or not chat_id:
        print("⚠ Notification Telegram ignorée : ~/.telegram_config absent "
              "ou section [telegram] incomplète.", file=sys.stderr)
        return
    if len(text) > TELEGRAM_MAX_CHARS:
        text = text[:TELEGRAM_MAX_CHARS - 20] + "\n… (tronqué)"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception as e:
        print(f"⚠ Notification Telegram échouée : {e}", file=sys.stderr)


def telegram_summary(changes: dict) -> str:
    """Message compact : une entrée par nouvelle offre, vente ou changement
    de prix."""
    n_new, n_sold = len(changes["new"]), len(changes["sold"])
    n_price = len(changes["price"])
    lines = [f"⌚ Omega Timekeeping.fr : {n_new} nouvelle(s) offre(s), "
             f"{n_sold} vendue(s), {n_price} changement(s) de prix"]
    for r in changes["new"]:
        lines.append(f"+ NOUVELLE : {r['title']} — {fmt_price(r['price'])}")
        if r.get("url"):
            lines.append(f"  {r['url']}")
    for r in changes["sold"]:
        lines.append(f"✖ VENDUE : {r['title']} (dernier prix {fmt_price(r['price'])})")
    for r, old in changes["price"]:
        lines.append(f"~ PRIX : {r['title']} — {fmt_price(old)} -> "
                     f"{fmt_price(r['price'])}")
        if r.get("url"):
            lines.append(f"  {r['url']}")
    return "\n".join(lines)


def main() -> int:
    migrate_legacy_csv(CSV_FILE)

    om_list = fetch_via_store_api()
    source = "API Store WooCommerce"
    if om_list is None:
        try:
            om_list = fetch_via_search_fallback()
            source = "repli recherche WordPress"
        except requests.RequestException as e:
            print(f"[ERROR] impossible de joindre {BASE} : {e}", file=sys.stderr)
            return 1

    if not om_list:
        print(f"[WARN] aucune montre Omega trouvée via {source} — "
              "le site a peut-être changé de structure, vérification manuelle nécessaire.")
        return 1

    changes = write(CSV_FILE, om_list)
    n_price, n_sold = len(changes["price"]) + len(changes["new"]), len(changes["sold"])
    horodatage = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Page HTML : toujours réécrite (état du jour)
    write_html_page(om_list, changes)

    parts = [f"{len(om_list)} Omega(s) trouvée(s)"]
    if n_price:
        parts.append(f"{n_price} changement(s) de prix")
    if n_sold:
        parts.append(f"{n_sold} vendue(s) depuis le dernier relevé")
    if not n_price and not n_sold:
        parts.append("aucun changement")

    print(f"[{horodatage}] ({source}) " + ", ".join(parts) + f" — {CSV_FILE}")

    # Telegram : seulement s'il y a du nouveau. Au tout premier relevé
    # (aucun historique), on évite d'inonder le canal : un seul message court.
    if changes["first_run"]:
        token, chat_id = load_telegram_config(Path(os.path.expanduser(TELEGRAM_CONFIG)))
        send_telegram(token, chat_id,
                      f"⌚ Omega Timekeeping.fr : suivi initialisé, "
                      f"{len(om_list)} montre(s) en ligne.")
    elif changes["new"] or changes["sold"] or changes["price"]:
        token, chat_id = load_telegram_config(Path(os.path.expanduser(TELEGRAM_CONFIG)))
        send_telegram(token, chat_id, telegram_summary(changes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
