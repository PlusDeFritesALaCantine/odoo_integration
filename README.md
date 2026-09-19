# odoo_integration_futurekawa

Module d'intégration entre le **backend central siège** (`backend_futurekawa`) et
**Odoo 17.0 Community**, pour que la Direction Commerciale et la Direction Qualité
disposent d'une vision unifiée des lots de café vert (stock, statut qualité,
alertes) directement dans l'ERP.

Ce module **ne modifie rien** dans `api_futurekawa`, `backend_futurekawa` ou
`MQTT_Broker` — il lit uniquement l'API HTTP déjà exposée par `backend_futurekawa`
et pousse les données vers Odoo via XML-RPC.

## Ce que fait ce module

- Réplique chaque lot FutureKawa dans Odoo comme un `stock.lot` traçable.
- **Remonte en sens inverse** les corrections saisies dans l'ERP (exploitation, entrepôt,
  date de stockage), avec arbitrage par horodatage — voir ci-dessous.
- Synchronise le statut du lot (`conforme` / `en_alerte` / `perime`).
- Poste une note dans le chatter du lot à chaque alerte qualité, et planifie une
  activité Odoo pour les cas sévères (lot périmé, mesure "critique").
- Tourne en boucle périodique (robuste aux coupures réseau ponctuelles d'un pays)
  et expose un déclenchement manuel pour la démo/le débogage.

## Architecture retenue

**Service Python/FastAPI externe** (et non un module Odoo custom exécuté côté
serveur Odoo) qui appelle Odoo via XML-RPC (`xmlrpc.client`, bibliothèque standard).

| | Module Odoo custom (ORM serveur) | Service externe (ce choix) |
|---|---|---|
| Déploiement | Copier dans les addons Odoo, redémarrer/mettre à jour l'instance à chaque évolution | Conteneur indépendant, `docker compose up`, aucun accès au serveur Odoo requis |
| Testabilité | Nécessite une instance Odoo (ou Odoo-bin de test) pour exécuter le moindre test | Testable hors-ligne — la brique Odoo est un simple client XML-RPC mockable (voir `tests/fakes.py`) |
| Couplage | Fort : dépend de la version Python embarquée par Odoo, du cycle de vie du serveur | Faible : un bug dans la synchro ne peut pas faire planter l'instance Odoo elle-même |
| Cohérence avec le projet | Rupture avec le pattern des 5 autres dépôts (services FastAPI indépendants) | Même pattern que `api_futurekawa`/`backend_futurekawa` : requirements.txt, Dockerfile, CI GitHub Actions, tests avec mocks |

Seul un **addon Odoo minimal** (`odoo_addon/x_futurekawa_stock/`) reste nécessaire,
dans les deux approches : Odoo n'a pas nativement de champs pour stocker le pays
d'origine, le statut qualité FutureKawa ou les dernières mesures IoT. Cet addon
n'ajoute aucun nouveau modèle, seulement des champs sur `stock.lot`.

## Les deux sens de synchronisation

La synchronisation n'allait que de FutureKawa vers Odoo : une correction saisie dans l'ERP
(mauvaise exploitation, lot déplacé d'entrepôt) était **écrasée au cycle suivant** par la
valeur FutureKawa. Une passe descendante la précède désormais.

| | |
|---|---|
| **Qui fait foi ?** | arbitrage par horodatage. Si `write_date` dépasse `x_futurekawa_derniere_sync_le` de plus de 5 s, la dernière main est humaine et **Odoo gagne** ; sinon c'est notre propre écriture et **FutureKawa gagne** |
| **Sur quels champs ?** | `x_futurekawa_exploitation`, `x_futurekawa_entrepot_id`, `x_futurekawa_date_stockage`. **Jamais** le statut ni les relevés (dérivés), ni le pays ni l'identifiant (ils portent la correspondance entre les deux systèmes) |
| **Dans quel ordre ?** | descendante **avant** montante. Sinon la passe montante réécrirait l'ancienne valeur dans la foulée et la correction humaine ferait un aller-retour pour rien |
| **Un lot créé dans Odoo ?** | ignoré. La création reste du ressort de FutureKawa ; Odoo est un miroir enrichi, pas une source. Rien n'est supprimé non plus |
| **Un champ vidé dans Odoo ?** | non propagé : on ne remonte pas un effacement |
| **En cas d'échec ?** | consigné dans `PaysSyncResult.erreurs`, le cycle va jusqu'au bout |

**Pourquoi 5 secondes de marge ?** Notre propre `write` met aussi `write_date` à jour. Sans
tolérance, chaque lot poussé serait aussitôt considéré comme modifié dans Odoo et repartirait
en sens inverse à chaque cycle.

Les remontées sont comptées dans `lots_remontes` du rapport de synchro
(`GET /sync/last-report`). En `dry_run`, elles sont comptées mais aucune écriture n'a lieu.

Fonctions concernées, toutes pures et testables sans Odoo :
`mapping.modifie_dans_odoo`, `mapping.champs_a_remonter`, `mapping.decoder_business_key`.


## Mapping des données

| Donnée FutureKawa | Champ Odoo | Type |
|---|---|---|
| `pays` + `id` du lot | `name` et `x_futurekawa_lot_id` (`"{pays}:{id}"`) | Char (clé métier unique) |
| — | `product_id` | Many2one vers un produit unique "café vert" (`default_code=CAFE-VERT`), commun aux 3 pays ; le pays est porté par les champs custom, pas par des variantes de produit |
| `pays` | `x_futurekawa_pays` | Selection |
| `exploitation` | `x_futurekawa_exploitation` | Char |
| `entrepot_id` | `x_futurekawa_entrepot_id` | Char |
| `date_stockage` | `x_futurekawa_date_stockage` | Date |
| `statut` (conforme/perime) + alertes actives | `x_futurekawa_statut` (conforme/en_alerte/perime) | Selection — `en_alerte` est calculé par ce module, `api_futurekawa` ne connaît que conforme/perime |
| dernière mesure hors seuil de l'entrepôt | `x_futurekawa_derniere_temperature`, `x_futurekawa_derniere_humidite`, `x_futurekawa_derniere_mesure_le` | Float / Float / Datetime — uniquement renseignés quand l'entrepôt a une alerte active (voir Limites connues) |
| alerte (lot périmé ou mesure hors seuil) | note dans le chatter (`message_post`) | — |
| alerte sévère (périmé ou mesure "critique") | activité planifiée (`activity_schedule`) | — |

La clé métier `"{pays}:{id}"` (et non l'id brut) sert à la fois de `name` Odoo et de
recherche d'idempotence : `stock.lot` impose une contrainte unique
`(product_id, company_id, name)`, et tous les lots partagent le même produit — deux
pays produisant chacun un lot d'id brut identique collisionneraient sinon.

Le module Quality (`quality.check`) n'est **pas** utilisé : uniquement des champs
custom sur `stock.lot`, pour rester compatible Community sans dépendance
supplémentaire.

**Note d'implémentation — `activity_schedule` en XML-RPC externe** : le mixin natif
`mail.activity.mixin.activity_schedule()` renvoie le recordset `mail.activity` créé,
qu'`xmlrpc.client` ne sait pas marshaller (seuls des types Python simples passent par
`execute_kw`). `odoo_addon/x_futurekawa_stock` expose donc un wrapper
`x_futurekawa_activity_schedule()` qui appelle `activity_schedule()` et ne renvoie que
les ids ; `app/odoo_client.py` appelle ce wrapper, jamais la méthode native directement.

## Déclenchement : boucle périodique (et non webhook)

`backend_futurekawa` est un agrégateur HTTP sans état ni persistance : il n'a
aujourd'hui aucun moyen de détecter "quelque chose a changé" ni d'émettre un
webhook, et le modifier pour ajouter cette capacité sortirait du périmètre de ce
module. Un déclenchement par **pull périodique** (`SYNC_INTERVAL_SECONDS`, 5 min
par défaut) est donc à la fois la seule option réalisable sans toucher au backend
existant, et la plus robuste face à un réseau d'entrepôt variable : chaque cycle
est indépendant et idempotent, un cycle en échec (Odoo ou un pays injoignable) se
rattrape automatiquement au cycle suivant sans infrastructure de retry dédiée.

`POST /sync` reste disponible en complément pour un déclenchement manuel (démo,
débogage) et comme point d'accroche si un vrai webhook `backend_futurekawa` est
ajouté plus tard.

## Prérequis

- Docker/Podman & Compose
- Une instance Odoo 17.0 Community accessible (URL, base, utilisateur, clé API) — soit
  l'instance locale fournie par `docker-compose.yml` (services `odoo`/`odoo_db`, pour le
  développement et la démo), soit une instance déjà hébergée en production
- Un produit Odoo créé au préalable avec `default_code=CAFE-VERT` (ou la valeur de
  `ODOO_DEFAULT_PRODUCT_DEFAULT_CODE`) — la synchronisation échoue explicitement
  (erreur fatale journalisée) tant qu'il n'existe pas

## Installation de l'addon Odoo

### Instance locale (fournie, dev/démo)

`docker-compose.yml` démarre une instance Odoo 17.0 dédiée (`odoo` + sa base `odoo_db`)
et monte automatiquement `odoo_addon/` dans son addons-path (`/mnt/extra-addons`) —
aucune copie manuelle nécessaire. Une seule commande initialise la base avec les
modules requis et le module custom, sans passer par l'assistant web :

```bash
podman-compose up -d odoo_db odoo   # (ou docker compose)
podman exec odoo-erp odoo -d futurekawa --db_host=odoo_db --db_user=odoo --db_password=odoo \
  -i stock,mail,x_futurekawa_stock --without-demo=all --stop-after-init
```

Cela crée la base `futurekawa` avec un utilisateur `admin`/`admin` par défaut (identique
au comportement d'une installation Odoo classique via CLI). Il reste à créer le produit
"café vert" (une fois, via XML-RPC ou l'UI `http://localhost:8069`) :

```bash
python3 -c "
import xmlrpc.client
url, db, user, pwd = 'http://localhost:8069', 'futurekawa', 'admin', 'admin'
uid = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common').authenticate(db, user, pwd, {})
models = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/object')
models.execute_kw(db, uid, pwd, 'product.product', 'create', [{
    'name': 'Café vert FutureKawa', 'default_code': 'CAFE-VERT', 'type': 'product', 'tracking': 'lot',
}])
"
```

**Après toute modification du code Python de `odoo_addon/`**, un `-u x_futurekawa_stock`
(comme ci-dessus, avec `-u` au lieu de `-i`) met à jour le schéma, mais le process Odoo
déjà démarré doit être redémarré (`podman restart odoo-erp`) pour ré-importer le code
Python modifié — le rechargement à chaud (`--dev=reload`) n'est pas activé sur cette image.

### Instance externe déjà hébergée (production)

Étape manuelle, à faire une fois sur l'instance Odoo cible (ce dépôt ne peut pas déployer
directement sur une instance distante) :

1. Copier `odoo_addon/x_futurekawa_stock/` dans le dossier addons de l'instance Odoo.
2. Redémarrer le serveur Odoo.
3. Activer le mode développeur, puis Apps > Mettre à jour la liste des applications.
4. Rechercher "FutureKawa - Suivi IoT sur les lots" et l'installer.
5. Créer le produit "café vert" avec `default_code=CAFE-VERT` (Inventaire > Produits).
6. Générer une clé API pour l'utilisateur d'intégration (Réglages > Utilisateurs >
   l'utilisateur concerné > onglet Sécurité du compte > Clés API).

## Lancement

```bash
cp .env.example .env
# renseigner ODOO_URL / ODOO_DB / ODOO_USERNAME / ODOO_API_KEY
# (instance locale : voir MQTT_Broker/../COMMANDS.md pour les valeurs prêtes à l'emploi)
podman-compose up -d --build   # ou docker compose up --build
```

Le service démarre sur `http://localhost:8003`. `backend_futurekawa` tourne nativement
sur l'hôte (venv, pas en conteneur — voir `COMMANDS.md` racine) : `BACKEND_FUTUREKAWA_URL`
pointe donc vers `http://host.containers.internal:8002` (Podman) — équivalent
`host.docker.internal` sous Docker Desktop — à condition que ce process écoute sur
`0.0.0.0` et non `127.0.0.1` (c'est le cas dans les commandes documentées).

## Mode dry-run

`ODOO_DRY_RUN=true` (ou `POST /sync?dry_run=true`) exécute tout le pipeline —
lecture de `backend_futurekawa`, recherche des lots existants dans Odoo, calcul des
statuts — sans jamais appeler `create`/`write`/`message_post`/`activity_schedule`.
Le rapport de synchronisation indique ce qui *aurait* été créé/mis à jour. Utile
pour valider la configuration (URL, clé API, produit) avant la première écriture
réelle.

## Développement local

```bash
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8003
```

## Endpoints

| Méthode | Route              | Description                                          |
|---------|---------------------|-------------------------------------------------------|
| GET     | `/health`           | Healthcheck                                            |
| POST    | `/sync?dry_run=`    | Déclenche une synchronisation immédiate, retourne le rapport |
| GET     | `/sync/last-report` | Dernier rapport de synchronisation (boucle ou manuel)  |

## Variables d'environnement

Voir `.env.example` pour la liste complète et les valeurs par défaut.

## Tests

51 tests, dont 16 sur la passe descendante (arbitrage, champs remontés, absence de PATCH inutile).

 manuels

```bash
# valider la configuration sans rien écrire dans Odoo
curl -X POST "http://localhost:8003/sync?dry_run=true" | jq

# synchronisation réelle
curl -X POST "http://localhost:8003/sync" | jq

# vérifier dans Odoo : Inventaire > Lots/Numéros de série, ouvrir un lot FutureKawa
# et consulter l'onglet "FutureKawa" + le chatter
```

## Tests automatisés

```bash
pip install -r requirements.txt
pytest
```

Aucun test ne nécessite d'instance Odoo réelle : `tests/fakes.py` fournit un double
en mémoire de l'API XML-RPC (`FakeOdooObject`/`FakeOdooCommon`), et `respx` mocke
les appels HTTP vers `backend_futurekawa`, sur le même principe que les tests
existants de `backend_futurekawa`.

## Intégration Docker Compose / CI

- `docker-compose.yml` suit le même format que les autres dépôts pour le service
  `odoo_integration_futurekawa` (`build: .`, `env_file: .env`, `restart: unless-stopped`).
  Deux services complémentaires (`odoo`, `odoo_db`) fournissent une instance Odoo 17.0
  locale pour le dev/la démo — en production, ils seraient remplacés par l'URL/DB/API
  key d'une instance déjà hébergée (voir `.env.example`), sans changer ce service.
- `.github/workflows/ci.yml` reproduit exactement le pipeline des 4 autres dépôts
  Python (`api_futurekawa`, `backend_futurekawa`) : checkout, Python 3.12, `pip
  install`, `pytest -q`. Le cahier des charges mentionne Jenkins, mais l'équipe a
  déjà documenté (voir le diaporama de soutenance) le choix de GitHub Actions pour
  les 5 dépôts existants — ce module suit la même convention par cohérence.
- L'addon Odoo (`odoo_addon/`) n'est build/testé par aucune CI : il n'existe aucune
  instance Odoo jetable dans ce pipeline, et son installation reste une étape
  manuelle documentée ci-dessus.

## Limites connues / évolutions possibles

- **Mesures IoT non répliquées en historique** : seules les dernières
  température/humidité d'un entrepôt *en alerte* sont poussées dans Odoo ; ce
  module ne réplique pas l'historique complet des relevés (hors périmètre —
  `backend_futurekawa` n'expose d'ailleurs pas d'endpoint "dernière mesure par
  entrepôt", seulement un résumé par pays).
- **Mouvement de stock non bloqué automatiquement** : sur alerte sévère, ce module
  poste une note et planifie une activité, mais ne bloque pas de `stock.quant` ni
  de `stock.picking` — cette action demanderait un workflow Odoo plus profond,
  disproportionné pour ce périmètre. Évolution possible si le besoin se confirme.
- **Un seul produit Odoo partagé** entre les 3 pays (le pays est porté par un champ
  custom). Des variantes de produit par pays/qualité restent une évolution possible.
- **Déduplication des notes/activités en mémoire du process** (`_derniere_alerte_notifiee`
  dans `app/sync.py`), remise à zéro si le service redémarre — même limite déjà
  assumée par `api_futurekawa` pour la dédup des emails d'alerte.
- **Aucune intégration avec l'app Quality** (`quality.check`) : choix assumé pour
  rester compatible Odoo Community sans dépendance supplémentaire.