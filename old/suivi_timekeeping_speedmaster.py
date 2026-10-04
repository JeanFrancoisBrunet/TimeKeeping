#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""suivi_timekeeping_speedmaster.py

Exécution UNIQUE (pas de boucle interne) : relève les Omega Speedmaster
disponibles sur https://timekeeping.fr et ajoute une ligne par montre dans un
CSV horodaté, uniquement si le prix a changé depuis le dernier relevé.

La récurrence quotidienne est assurée par cron, PAS par ce script :
    /tool cron add 0 9 * * * :: suivi Speedmaster timekeeping.fr

Stratégie (dans cet ordre) :
1. API Store WooCommerce (/wp-json/wc/store/v1/products) — publique, sans
   authentification, retourne du JSON propre par catégorie. C'est la voie
   normale sur un site WooCommerce.
2. Repli : si l'API est désactivée/absente (404/403), on récupère les URLs
   de fiches produits via la recherche WordPress native (?s=speedmaster,
   HTML côté serveur, pas de JS) puis on lit les balises meta
   (product:price:amount, product:availability) de chaque fiche.

NOTE IMPORTANTE : la page de listing https://timekeeping.fr/collections/omega
charge sa grille de produits en JavaScript après le chargement initial — elle
est donc volontairement ignorée ici, un scraping HTML statique n'y verrait
aucun produit.

Dépendances : requests
    pip install requests --break-system-packages
"""

import csv
import datetime
import re
import sys
from pathlib import Path

import requests

BASE = "https://timekeeping.fr"
STORE_API = f"{BASE}/wp-json/wc/store/v1/products"
SEARCH_URL = f"{BASE}/"
CSV_FILE = Path(__file__).parent / "speedmaster_timekeeping.csv"
USER_AGENT = "Mozilla/5.0 (compatible; JFBBot/1.0; +https://jfbconseil14.com)"
TIMEOUT = 30

META_PRICE_RE = re.compile(r'meta-product:price:amount:\s*([\d.]+)', re.I)
META_AVAIL_RE = re.compile(r'meta-product:availability:\s*(.+)', re.I)
META_TITLE_RE = re.compile(r'^title:\s*(.+)', re.I | re.M)


def _headers():
    return {"User-Agent": USER_AGENT}


def fetch_via_store_api() -> list[dict] | None:
    """Tente l'API Store WooCommerce. Retourne None si indisponible
    (l'appelant doit alors utiliser le repli), ou la liste des Speedmaster."""
    try:
        resp = requests.get(
            STORE_API,
            params={"category": "omega", "per_page": 100, "search": "speedmaster"},
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
        if "speedmaster" not in name.lower():
            continue
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
            "ref": str(item.get("sku") or item.get("id", "N/A")),
            "price": price or "N/A",
            "stock": stock,
        })
    return rows


def fetch_via_search_fallback() -> list[dict]:
    """Repli : recherche WordPress native (HTML statique) pour trouver les
    URLs de fiches Speedmaster, puis lit les balises meta de chaque fiche."""
    resp = requests.get(
        SEARCH_URL, params={"s": "speedmaster"}, headers=_headers(), timeout=TIMEOUT
    )
    resp.raise_for_status()
    urls = sorted(set(re.findall(
        r'https://timekeeping\.fr/products/[a-z0-9\-]*speedmaster[a-z0-9\-]*',
        resp.text, re.I,
    )))

    rows = []
    for url in urls:
        try:
            page = requests.get(url, headers=_headers(), timeout=TIMEOUT)
            page.raise_for_status()
        except requests.RequestException:
            continue
        price_m = META_PRICE_RE.search(page.text)
        avail_m = META_AVAIL_RE.search(page.text)
        title_m = META_TITLE_RE.search(page.text)
        rows.append({
            "title": title_m.group(1).strip() if title_m else url,
            "ref": url.rsplit("/", 1)[-1],
            "price": price_m.group(1) if price_m else "N/A",
            "stock": avail_m.group(1).strip() if avail_m else "N/A",
        })
    return rows


SOLD_STOCK_LABEL = "Vendu (retiré du site)"


def _last_known_state(csv_path: Path) -> dict[str, dict]:
    """Retourne, pour chaque référence, les infos (titre, prix, stock) de sa
    toute dernière ligne connue dans le CSV (le fichier est append-only et
    trié chronologiquement, donc la dernière occurrence = l'état le plus
    récent connu pour cette référence)."""
    if not csv_path.exists():
        return {}
    last: dict[str, dict] = {}
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            last[row["reference"]] = {
                "title": row["title"],
                "price": row["price_eur"],
                "stock": row["stock"],
            }
    return last


def write(csv_path: Path, data: list[dict]) -> tuple[int, int]:
    """Ajoute une ligne par montre dont le prix a changé depuis le dernier
    relevé pour cette référence, PLUS une ligne pour chaque référence
    précédemment connue qui a disparu du relevé du jour (vente probable),
    tant qu'elle n'a pas déjà été marquée comme vendue auparavant.

    Retourne (nb_maj_prix, nb_ventes_detectees)."""
    file_exists = csv_path.exists()
    previous = _last_known_state(csv_path)
    now = datetime.datetime.now().isoformat(sep=" ", timespec="seconds")

    price_updates = [
        row for row in data
        if previous.get(row["ref"], {}).get("price") != row["price"]
    ]

    current_refs = {row["ref"] for row in data}
    sold_rows = []
    for ref, info in previous.items():
        if ref in current_refs:
            continue  # toujours présente sur le site
        if info["stock"] == SOLD_STOCK_LABEL:
            continue  # déjà signalée vendue lors d'un run précédent
        sold_rows.append({
            "title": info["title"],
            "ref": ref,
            "price": info["price"],
            "stock": SOLD_STOCK_LABEL,
        })

    to_write = price_updates + sold_rows
    if not to_write:
        return 0, 0

    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["datetime", "title", "reference", "price_eur", "stock"])
        for row in to_write:
            writer.writerow([now, row["title"], row["ref"], row["price"], row["stock"]])
    return len(price_updates), len(sold_rows)


def main() -> int:
    sm_list = fetch_via_store_api()
    source = "API Store WooCommerce"
    if sm_list is None:
        try:
            sm_list = fetch_via_search_fallback()
            source = "repli recherche WordPress"
        except requests.RequestException as e:
            print(f"[ERROR] impossible de joindre {BASE} : {e}", file=sys.stderr)
            return 1

    if not sm_list:
        print(f"[WARN] aucun Speedmaster trouvé via {source} — "
              "le site a peut-être changé de structure, vérification manuelle nécessaire.")
        return 1

    n_price, n_sold = write(CSV_FILE, sm_list)
    horodatage = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    parts = [f"{len(sm_list)} Speedmaster(s) trouvés"]
    if n_price:
        parts.append(f"{n_price} changement(s) de prix")
    if n_sold:
        parts.append(f"{n_sold} vendue(s) depuis le dernier relevé")
    if not n_price and not n_sold:
        parts.append("aucun changement")

    print(f"[{horodatage}] ({source}) " + ", ".join(parts) + f" — {CSV_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
