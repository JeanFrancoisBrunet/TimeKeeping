# suivi_timekeeping_omega.py

Script autonome de veille horlogère, exécuté sur un Raspberry Pi 5, qui surveille les montres **Omega** en vente sur le site **timekeeping.fr** : nouvelles offres, ventes et changements de prix.

## Principe
Le script interroge l'**API Store WooCommerce** du site (`/wp-json/wc/store/v1/products`), publique et sans authentification, qui renvoie du JSON propre pour la catégorie `omega`. Il ne lit que des données que le site expose lui-même à tous ses visiteurs.

Exécution **unique** à chaque lancement (pas de boucle interne) : la récurrence est assurée par cron, pas par le script.

## Fonctionnement
1. Relevé de **toutes** les montres Omega actuellement en ligne (Speedmaster, Seamaster, Constellation, De Ville, etc.).
2. Comparaison avec la dernière version connue de chaque fiche dans le CSV (identifiée par son `sku_woocommerce`) :
   - **nouvelle offre** : fiche jamais vue, ou réapparue après avoir été marquée vendue ;
   - **changement de prix** : prix différent du dernier relevé ;
   - **vente probable** : fiche connue qui a disparu du site (marquée « Vendu (retiré du site) », une seule fois).
3. Ajout au CSV (historique, ajout seul) **uniquement** des nouveautés, changements de prix et ventes.
4. Réécriture de la page HTML des montres actuellement en ligne.
5. Notification Telegram s'il y a eu au moins une nouveauté, une vente ou un changement de prix.

## Stratégie de collecte (dans cet ordre)
1. **API Store WooCommerce** : voie normale sur un site WooCommerce, filtrée sur la catégorie `omega`.
2. **Repli** si l'API est désactivée ou absente (réponse autre que 200) : recherche WordPress native (`/?s=omega`, HTML rendu côté serveur), puis lecture des balises meta de chaque fiche produit (`product:price:amount`, `product:availability`).

> La page de listing `https://timekeeping.fr/collections/omega` charge sa grille en JavaScript : elle est volontairement ignorée, un scraping HTML statique n'y verrait aucun produit.

## Mise en place (une seule fois)
1. Installer la dépendance (voir « Installation »).
2. Placer le script dans son dossier de travail, par exemple `~/Projects/Groq_agent/Timekeeping/` : le CSV et la page HTML sont créés **à côté du script**.
3. **Telegram (facultatif)** : réutilise le même bot et le même fichier que `emails_scan.py` et `recherche_immobilier.py` (`~/.telegram_config`, section `[telegram]`, clés `token_groq`/`chat_id`) — rien à reconfigurer si l'un des deux l'utilise déjà.
4. Lancer une première fois à la main, puis programmer cron :
   ```
   0 9 * * * cd /home/jfbrunet/Projects/Groq_agent/Timekeeping && python3 suivi_timekeeping_omega.py >> cron_omega.log 2>&1
   ```

Aucun fichier de configuration : les réglages (URL, fichiers, chemin Telegram) sont des constantes en tête du script.

## Les deux références du CSV
Le site expose deux informations à ne pas confondre :
- **`sku_woocommerce`** : identifiant interne de gestion côté vendeur (SKU WooCommerce, à défaut l'ID produit en base, ou, en mode repli, le slug de l'URL). Il est saisi indépendamment du titre, peut être vide, réutilisé ou arbitraire : il ne sert qu'à suivre **une même fiche** d'un relevé à l'autre.
- **`ref.`** : la référence horlogère **annoncée dans le titre** (ex. « ref 2849 »), extraite par expression régulière : motif « ref XXX » d'abord, à défaut un nombre en fin de titre. C'est ce que voit le client ; elle vaut `N/A` si le titre n'en contient pas.

## Format du CSV (`omega_timekeeping.csv`)
```
datetime,title,sku_woocommerce,ref.,price_eur,stock
```
Journal en **ajout seul**, chronologique : la dernière ligne d'un `sku_woocommerce` donne son état le plus récent. Une vente apparaît avec `stock = Vendu (retiré du site)` et le dernier prix connu. Un ancien CSV au format `reference` est migré automatiquement une seule fois.

## Page HTML (`omega_timekeeping_disponibles.html`)
Réécrite à **chaque** exécution (état du jour, pas un journal) : toutes les montres actuellement en ligne, triées par **prix croissant**, avec titre, référence, prix, stock et un vrai lien cliquable « Voir la fiche » vers le site. À ouvrir dans un navigateur.
- Pastille **Nouveau** sur les montres apparues lors du dernier relevé.
- Ancien prix affiché en rouge « (auparavant … €) » après un changement de prix.

## Notification Telegram
Un message est envoyé après un relevé qui contient au moins une nouveauté, une vente ou un changement de prix :
```
⌚ Omega timekeeping.fr : 1 nouvelle(s) offre(s), 1 vendue(s), 1 changement(s) de prix
+ NOUVELLE : <titre> — 2 500 €
  <lien de la fiche>
✖ VENDUE : <titre> (dernier prix 9 990 €)
~ PRIX : <titre> — 8 490 € -> 7 900 €
  <lien de la fiche>
```
- Les jours **sans changement**, aucun message n'est envoyé.
- Au **tout premier relevé** (aucun historique), un seul message court « suivi initialisé » évite d'inonder le canal avec toutes les montres.
- Un message trop long est tronqué à 4 000 caractères (limite Telegram : 4 096).
- Si `~/.telegram_config` ou sa section `[telegram]` est absent, la notification est ignorée (avertissement sur stderr) : ce n'est jamais bloquant.

## À savoir
- Le journal cron indique une ligne par exécution, par exemple :
  `[2026-10-05 09:00:03] (API Store WooCommerce) 12 Omega(s) trouvée(s), aucun changement — …/omega_timekeeping.csv`
  Le décompte « changement(s) de prix » additionne les nouvelles offres et les vrais changements de prix.
- Premier test conseillé : lancer le script deux fois de suite à la main. La 2e fois doit annoncer « aucun changement » et ne rien envoyer sur Telegram.
- Testez avec un environnement proche de celui de cron (qui ne reprend pas votre session interactive) :
  ```bash
  env -i HOME="$HOME" python3 suivi_timekeeping_omega.py
  ```
  Si `~/.telegram_config` n'est pas trouvé, ajoutez une ligne `HOME=/home/xxx` en tête de votre crontab, avant la ligne de commande.
- Code de retour `1` si le site est injoignable ou si aucune montre n'est trouvée (message `[ERROR]` ou `[WARN]` : structure du site peut-être modifiée, vérification manuelle nécessaire).
- Une page HTML n'est écrite que si un relevé a réussi : en cas d'échec, la version précédente reste en place.

## Limites à connaître
- Une **vente** est une disparition du site : le script ne peut pas distinguer une vente d'un simple retrait de l'annonce par le vendeur. D'où le terme « vente probable ».
- Une fiche **repassée en vente** après avoir été marquée vendue est comptée comme une nouvelle offre.
- Le `sku_woocommerce` diffère entre l'API et le mode repli (en repli, c'est le slug de l'URL) : si le script bascule d'un mode à l'autre, toutes les fiches peuvent apparaître comme « nouvelles » puis « vendues ». Le basculement ne doit se produire qu'en cas de panne de l'API.
- L'API est interrogée avec `per_page=100` : au-delà de 100 montres Omega en ligne, les suivantes ne seraient pas relevées (très loin du volume actuel, une douzaine).
- Un changement de prix n'est détecté qu'au rythme de cron (une fois par jour) : plusieurs changements dans la même journée n'en laissent qu'un.
- La page HTML ne montre que l'état du jour. L'historique complet (prix passés, ventes) est dans le CSV.

## Installation
```bash
pip install requests --break-system-packages
```

## Arborescence du projet
```
suivi_timekeeping_omega.py             script principal
omega_timekeeping.csv                  journal des nouveautés, changements de prix et ventes (ajout seul)
omega_timekeeping_disponibles.html     montres actuellement en ligne, avec liens cliquables ;
                                       réécrit à chaque exécution (pas un journal) — à privilégier pour la consultation
cron_omega.log                         sortie cron (si configuré en >> cron_omega.log 2>&1)
```

## Projets associés
- **`recherche_immobilier.py`** — veille des annonces immobilières à ... via les alertes e-mail SeLoger. Projet distinct, avec lequel celui-ci partage le bot Telegram (`~/.telegram_config`) et le même principe d'exécution unique pilotée par cron.
- **`emails_scan.py`** — scan/classification des 3 boîtes email JFBConseils. Partage également le bot Telegram.
- **`suivi_timekeeping_speedmaster.py`** — ancienne version, limitée à un seul modèle (SpeedMaster) : remplacée par ce script.

## Auteur
Jean-François Brunet – JFBConseils - Octobre 2026
