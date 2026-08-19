{
    "name": "FutureKawa - Suivi IoT sur les lots",
    "version": "17.0.1.0.0",
    "category": "Inventory",
    "summary": "Champs de suivi FutureKawa (pays, statut qualité, dernières mesures IoT) sur les lots de stock",
    "description": """
Ajoute à stock.lot les champs nécessaires à la synchronisation avec le SI FutureKawa
(module d'intégration odoo_integration_futurekawa) : pays d'origine, entrepôt,
statut qualité (conforme / en alerte / périmé) et dernières mesures température/humidité
remontées par le module IoT. N'introduit aucun nouveau modèle : uniquement des champs
additionnels sur stock.lot, plus mail.thread/mail.activity.mixin pour permettre au
module d'intégration de poster des messages et planifier des activités sur les lots.
""",
    "author": "FutureKawa",
    "license": "LGPL-3",
    "depends": ["stock", "mail"],
    "data": [
        "views/stock_lot_views.xml",
    ],
    "installable": True,
    "application": False,
}