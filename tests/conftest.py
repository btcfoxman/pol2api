import os
from pathlib import Path
import tempfile

os.environ["POL_API_KEY"] = "test-api-key"
os.environ["POL_ADMIN_TOKEN"] = "test-admin-key"
os.environ["POL_DATABASE_PATH"] = str(
    Path(tempfile.mkdtemp(prefix="pol-import-test-")) / "db.sqlite"
)
