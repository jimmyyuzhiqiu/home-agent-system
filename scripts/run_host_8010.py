import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.app import app, ensure_schema_compat

with app.app_context():
    ensure_schema_compat()

app.run(host='127.0.0.1', port=8010)
