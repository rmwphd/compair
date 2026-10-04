import os
import secrets
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "compair.db"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
# A random fallback means admin sessions reset on restart; set SECRET_KEY in production.
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
# Set to 1 when served over HTTPS (e.g. behind Cloudflare Tunnel) so cookies are Secure.
SECURE_COOKIES = os.environ.get("SECURE_COOKIES", "0") == "1"
# New participant sessions per minute from one IP (IPs are held in memory only, never stored).
# Raise it if a large group shares one network (e.g. a classroom behind one NAT).
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "120"))
