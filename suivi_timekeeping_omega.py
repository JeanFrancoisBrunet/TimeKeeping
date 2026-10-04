#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""suivi_timekeeping_omega.py

Exécution UNIQUE (pas de boucle interne) : relève TOUTES les montres Omega
disponibles sur https://timekeeping.fr (Speedmaster, Seamaster, Constellation,
De Ville, etc.) et ajoute une ligne par montre dans un CSV horodaté,
uniquement si le prix a changé depuis le dernier relevé.

Ce script remplace suivi_timekeeping_speedmaster.py, qui se limitait à un seul modèle.

La récurrence quotidienne est assurée par cron, PAS par ce script :
    0 9 * * * suivi Omega timekeeping.fr

Stratégie (dans cet ordre) :
1. API Store WooCommerce (/wp-json/wc/store/v1/products) — publique, sans
   authentification, retourne du JSON propre par catégorie. C'est la voie
   normale sur un site WooCommerce. On filtre sur la catégorie "omega".
2. Repli : si l'API est désactivée/absente (404/403), on récupère les URLs
   de fiches produits via la recherche WordPress native (?s=omega,
   HTML côté serveur, pas de JS) puis on lit les balises meta
   (product:price:amount, product:availability) de chaque fiche.

NOTE IMPORTANTE : la page de listing https://timekeeping.fr/collections/omega
charge sa grille de produits en JavaScript après le chargement initial — 
elle est donc volontairement ignorée ici, un scraping HTML statique n'y verrait
aucun produit.

NOTE SUR LES COLONNES DE RÉFÉRENCE (CSV) :
Le site expose deux informations bien distinctes qu'il ne faut pas confondre :
- "sku_woocommerce" : l'identifiant interne renvoyé par le site (SKU
  WooCommerce, ou à défaut l'ID produit en base, ou en mode de repli le
  slug de l'URL). C'est un identifiant de GESTION côté vendeur, saisi/généré
  indépendamment du titre, pas fiable comme référence horlogère : il peut
  être vide, réutilisé d'une ancienne fiche, ou totalement arbitraire. On le
  garde uniquement pour détecter de façon stable les changements de prix
  d'une même fiche d'un relevé à l'autre.
- "ref." : la référence telle qu'annoncée dans le TITRE du
  produit (ex. "ref 2849"), extraite par regex. C'est ce que voit vraiment
  le client sur le site — mais elle peut être absente (titre sans "ref").

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
CSV_FILE = Path(__file__).parent / "omega_timekeeping.csv"
USER_AGENT = "Mozilla/5.0 (compatible; JFBBot/1.0; +https://jfbconseil14.com)"
TIMEOUT = 30

META_PRICE_RE = re.compile(r'meta-product:price:amount:\s*([\d.]+)', re.I)
META_AVAIL_RE = re.compile(r'meta-product:availability:\s*(.+)', re.I)
META_TITLE_RE = re.compile(r'^title:\s*(.+)', re.I | re.M)
PRODUCT_URL_RE = re.compile(r'https://timekeeping\.fr/products/[a-z0-9\-]+', re.I)

# Référence "annoncée" : on cherche d'abord un motif "ref XXX" explicite dans
# le titre (cas le plus fréquent et le plus fiable) ; à défaut, on retombe
# sur un éventuel nombre de référence en fin de titre (ex. "Chronomètre 2367").
REF_KEYWORD_RE = re.compile(r'\bref\.?\s*([A-Za0-9][\w./-]*)', re.IGNORECASE)
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
    (l'appelant doit alors utiliser le repli), ou la liste de toutes les
    montres Omega."""
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
            # Pas de vrai SKU disponible en mode de repli (pas d'appel à
            # l'API Store) : on utilise le slug de l'URL comme identifiant
            # interne stable pour le suivi des prix.
            "sku_woocommerce": url.rsplit("/", 1)[-1],
            "ref.": extract_reference_from_title(title),
            "price": price_m.group(1) if price_m else "N/A",
            "stock": avail_m.group(1).strip() if avail_m else "N/A",
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


def write(csv_path: Path, data: list[dict]) -> tuple[int, int]:
    """Ajoute une ligne par montre dont le prix a changé depuis le dernier
    relevé pour cette fiche (identifiée par sku_woocommerce), PLUS une ligne
    pour chaque fiche précédemment connue qui a disparu du relevé du jour
    (vente probable), tant qu'elle n'a pas déjà été marquée comme vendue auparavant.

    Retourne (nb_maj_prix, nb_ventes_detectees)."""
    file_exists = csv_path.exists()
    previous = _last_known_state(csv_path)
    now = datetime.datetime.now().isoformat(sep=" ", timespec="seconds")

    price_updates = [
        row for row in data
        if previous.get(row["sku_woocommerce"], {}).get("price") != row["price"]
    ]

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

    to_write = price_updates + sold_rows
    if not to_write:
        return 0, 0

    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["datetime", "title", "sku_woocommerce",
                              "ref.", "price_eur", "stock"])
        for row in to_write:
            writer.writerow([now, row["title"], row["sku_woocommerce"],
                              row["ref."], row["price"], row["stock"]])
    return len(price_updates), len(sold_rows)


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

    n_price, n_sold = write(CSV_FILE, om_list)
    horodatage = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    parts = [f"{len(om_list)} Omega(s) trouvée(s)"]
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
