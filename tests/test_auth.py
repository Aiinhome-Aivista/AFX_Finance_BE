import jwt

from app.db import connect


def test_register_validation_and_duplicate(client):
    bad = client.post("/api/auth/register", json={"email": "nope", "display_name": "A", "password": "x"})
    assert bad.status_code == 422
    weak = client.post("/api/auth/register", json={"email": "a@b.co", "display_name": "Ann", "password": "allletters"})
    assert weak.status_code == 422
    ok = client.post("/api/auth/register", json={"email": "Dup@Example.com", "display_name": "Ann", "password": "Passw0rdX"})
    assert ok.status_code == 201
    dup = client.post("/api/auth/register", json={"email": "dup@example.com", "display_name": "Ann", "password": "Passw0rdX"})
    assert dup.status_code == 409


def test_password_is_hashed(client, cfg):
    client.post("/api/auth/register", json={"email": "h@example.com", "display_name": "Hash", "password": "Passw0rdX"})
    conn = connect(cfg)
    with conn.cursor() as cur:
        cur.execute("SELECT password_hash FROM users WHERE email='h@example.com'")
        h = cur.fetchone()["password_hash"]
    conn.close()
    assert "Passw0rdX" not in h and h.startswith("pbkdf2:sha256")


def test_login_and_generic_failure(client):
    client.post("/api/auth/register", json={"email": "l@example.com", "display_name": "Lee", "password": "Passw0rdX"})
    ok = client.post("/api/auth/login", json={"email": "l@example.com", "password": "Passw0rdX"})
    assert ok.status_code == 200 and ok.get_json()["token"]
    wrong = client.post("/api/auth/login", json={"email": "l@example.com", "password": "bad"})
    unknown = client.post("/api/auth/login", json={"email": "zzz@example.com", "password": "bad"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.get_json()["error"]["message"] == unknown.get_json()["error"]["message"]


def test_protected_routes_require_token(client):
    for url in ("/api/domains", "/api/auth/me", "/api/apps", "/api/nodes/account", "/api/domains/working-capital-treasury/graph"):
        assert client.get(url).status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_tampered_and_expired_tokens_rejected(client, api):
    forged = jwt.encode({"sub": "1", "jti": "x", "exp": 9999999999}, "wrong-secret", algorithm="HS256")
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_logout_revokes_token(api):
    assert api.get("/api/auth/me").status_code == 200
    assert api.post("/api/auth/logout").status_code == 200
    assert api.get("/api/auth/me").status_code == 401


def test_audit_log_written(api, cfg):
    conn = connect(cfg)
    with conn.cursor() as cur:
        cur.execute("SELECT action FROM audit_logs ORDER BY id")
        actions = [r["action"] for r in cur.fetchall()]
    conn.close()
    assert "auth.register" in actions
