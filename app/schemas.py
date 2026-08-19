from datetime import datetime

from pydantic import BaseModel


class PaysSyncResultOut(BaseModel):
    pays: str
    status: str
    lots_crees: int
    lots_mis_a_jour: int
    messages_postes: int
    activites_planifiees: int
    erreurs: list[str]

    model_config = {"from_attributes": True}


class SyncReportOut(BaseModel):
    started_at: datetime
    finished_at: datetime | None
    dry_run: bool
    product_id: int | None
    per_pays: list[PaysSyncResultOut]
    fatal_error: str | None

    model_config = {"from_attributes": True}