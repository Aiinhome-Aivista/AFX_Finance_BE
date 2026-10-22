from dotenv import load_dotenv


from app import create_app  # noqa: E402
from flask_cors import CORS

load_dotenv()
app = create_app()

CORS(app) 

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
