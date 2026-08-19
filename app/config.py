import os

from dotenv import load_dotenv

load_dotenv()

def _bool_env(name, default):
    value = os.getenv(name, default)
    return value.strip().lower() in {"1", "true", "on"}

ODOO_URL = os.getenv("ODOO_URL", "")
ODOO_DB = os.getenv("ODOO_DB", "")
ODOO_USERNAME = os.getenv("ODOO_USERNAME", "")
ODOO_API_KEY = os.getenv("ODOO_API_KEY", "")
ODOO_TIMEOUT_SECONDS = float(os.getenv("ODOO_TIMEOUT_SECONDS", "10"))
ODOO_DRY_RUN = _bool_env("ODOO_DRY_RUN", "false")

ODOO_DEFAULT_PRODUCT_DEFAULT_CODE = os.getenv("ODOO_DEFAULT_PRODUCT_DEFAULT_CODE", "CAFE-VERT")
ODOO_ACTIVITY_USER_LOGIN = os.getenv("ODOO_ACTIVITY_USER_LOGIN", "")
BACKEND_FUTUREKAWA_URL = os.getenv("BACKEND_FUTUREKAWA_URL", "http://backend_futurekawa:8002")
BACKEND_FUTUREKAWA_TIMEOUT_SECONDS = float(os.getenv("BACKEND_FUTUREKAWA_TIMEOUT_SECONDS", "5"))
SYNC_INTERVAL_SECONDS = int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))

API_PORT = int(os.getenv("API_PORT", "8003"))