import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import config
from .schemas import SyncReportOut
# Dépôt autonome : le paquet s'appelle `app`, pas `odoo.app`. L'ancien import
# supposait l'arborescence de backend_futurekawa et faisait planter le conteneur
# au démarrage (ModuleNotFoundError: No module named 'odoo'), alors même que
# l'image se construisait et que les 35 tests passaient.
from app.sync import run_sync

logger = logging.getLogger(__name__)

_last_report: SyncReportOut | None = None


async def _boucle_synchronisation():
    global _last_report
    while True:
        try:
            rapport = await asyncio.to_thread(run_sync)
            _last_report = SyncReportOut.model_validate(rapport)
        except Exception:
            logger.exception("Erreur durant la synchronisation périodique FutureKawa -> Odoo")
        await asyncio.sleep(config.SYNC_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tache = asyncio.create_task(_boucle_synchronisation())
    yield
    tache.cancel()


app = FastAPI(title="FutureKawa — Intégration Odoo", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/sync", response_model=SyncReportOut)
async def declencher_sync(dry_run: bool | None = None):
    global _last_report
    rapport = await asyncio.to_thread(run_sync, dry_run)
    _last_report = SyncReportOut.model_validate(rapport)
    return _last_report


@app.get("/sync/last-report", response_model=SyncReportOut | None)
def dernier_rapport():
    return _last_report