from . import fields, models

PAYS_SELECTION = [
    ("bresil", "Brésil"),
    ("equateur", "Équateur"),
    ("colombie", "Colombie"),
]

STATUT_SELECTION = [
    ("conforme", "Conforme"),
    ("en_alerte", "En alerte"),
    ("perime", "Périmé"),
]


class StockLot(models.Model):
    # Re-declaring mail.thread/mail.activity.mixin here is a no-op if stock.lot
    # already has them on this instance, and guarantees message_post/activity_schedule
    # work regardless of version/edition for the odoo_integration_futurekawa sync service.
    # _name is required alongside a multi-value _inherit: without it Odoo defines a new
    # model named after the Python class instead of extending stock.lot.
    _name = "stock.lot"
    _inherit = ["stock.lot", "mail.thread", "mail.activity.mixin"]

    x_futurekawa_lot_id = fields.Char(
        string="Id FutureKawa",
        copy=False,
        help="Clé métier '{pays}:{id}' utilisée par le module de synchronisation "
        "pour retrouver ce lot de façon idempotente.",
    )
    x_futurekawa_pays = fields.Selection(PAYS_SELECTION, string="Pays FutureKawa")
    x_futurekawa_exploitation = fields.Char(string="Exploitation")
    x_futurekawa_entrepot_id = fields.Char(string="Entrepôt")
    x_futurekawa_date_stockage = fields.Date(string="Date de stockage")
    x_futurekawa_statut = fields.Selection(
        STATUT_SELECTION, string="Statut FutureKawa", default="conforme"
    )
    x_futurekawa_derniere_temperature = fields.Float(string="Dernière température (°C)")
    x_futurekawa_derniere_humidite = fields.Float(string="Dernière humidité (%)")
    x_futurekawa_derniere_mesure_le = fields.Datetime(string="Dernière mesure le")
    x_futurekawa_derniere_sync_le = fields.Datetime(string="Dernière synchro le")

    _sql_constraints = [
        (
            "x_futurekawa_lot_id_uniq",
            "unique(x_futurekawa_lot_id)",
            "Un lot avec cet identifiant FutureKawa existe déjà.",
        ),
    ]

    def x_futurekawa_activity_schedule(self, act_type_xmlid, summary, note, user_id=None):
        """Wrapper XML-RPC-safe autour de activity_schedule().

        Le mixin natif mail.activity.mixin.activity_schedule() renvoie le recordset
        mail.activity créé, que xmlrpc.client ne sait pas marshaller (seuls des types
        Python simples passent par execute_kw) : un appel externe direct à
        activity_schedule() échoue donc systématiquement avec
        "cannot marshal <class 'odoo.api.mail.activity'> objects". Ce wrapper ne renvoie
        que les ids (liste d'int), consommable par odoo_integration_futurekawa.
        """
        vals = {"user_id": user_id} if user_id else {}
        activities = self.activity_schedule(
            act_type_xmlid=act_type_xmlid, summary=summary, note=note, **vals
        )
        return activities.ids