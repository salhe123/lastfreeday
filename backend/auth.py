import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from db import connect

SECRET = os.getenv("JWT_SECRET", "lastday-dev-jwt-secret-key-32bytes")
ALGO = "HS256"
bearer = HTTPBearer(auto_error=False)


def login(username: str, password: str) -> dict:
    conn = connect()
    row = conn.execute(
        "SELECT * FROM users WHERE username = ? AND password = ?",
        (username, password),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(401, "bad credentials")
    payload = {
        "sub": row["username"],
        "role": row["role"],
        "exp": datetime.now(timezone.utc) + timedelta(hours=12),
    }
    token = jwt.encode(payload, SECRET, algorithm=ALGO)
    return {"token": token, "username": row["username"], "role": row["role"]}


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
    if creds is None:
        raise HTTPException(401, "login required")
    try:
        data = jwt.decode(creds.credentials, SECRET, algorithms=[ALGO])
    except jwt.PyJWTError as exc:
        raise HTTPException(401, "invalid token") from exc
    return {"username": data.get("sub"), "role": data.get("role")}


def require_roles(*roles: str):
    def _inner(user: dict = Depends(current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(403, f"role {user['role']} cannot do this")
        return user

    return _inner
