import urllib.parse
from dotenv import load_dotenv

from app import create_app  # noqa: E402
from flask_cors import CORS

load_dotenv()

# Port is defined here in app.py
PORT = 5007

app = create_app()
CORS(app)

# Synchronize BASE_URL port from app.py
cfg = app.config.get("APP_CONFIG")
if cfg:
    cfg.PORT = PORT
    if cfg.BASE_URL:
        parsed = urllib.parse.urlparse(cfg.BASE_URL)
        scheme = parsed.scheme or "http"
        host = parsed.hostname or "127.0.0.1"
        cfg.BASE_URL = f"{scheme}://{host}:{PORT}"
    else:
        cfg.BASE_URL = f"http://127.0.0.1:{PORT}"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True)

