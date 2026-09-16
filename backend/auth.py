from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

import jwt
from bson import ObjectId
from fastapi import Depends, HTTPException, Request, Response, status
from jwt import InvalidTokenError
from pydantic import BaseModel, Field
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pymongo.errors import DuplicateKeyError

from config import Settings

COOKIE_NAME = "hedgehoog_session"
JWT_ISSUER = "hedgehoog"
JWT_AUDIENCE = "hedgehoog-web"
password_hasher = PasswordHash.recommended()
# Unknown logins still perform a password verification to avoid a cheap timing oracle.
DUMMY_HASH = password_hasher.hash("not-a-real-user-password")


class LoginRequest(BaseModel):
    login: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class UserResponse(BaseModel):
    id: str
    login: str
    role: Literal["admin", "user"]


def public_user(user: dict) -> UserResponse:
    return UserResponse(id=str(user["_id"]), login=user["login"], role=user["role"])


def seed_admin(database, settings: Settings):
    users = database.users
    users.create_index("login", unique=True)
    existing = users.find_one({"login": settings.admin_login})
    if existing is not None:
        if settings.environment == "production" and password_hasher.verify("admin", existing["password_hash"]):
            raise ValueError("The stored admin account still uses the development password; rotate it before production")
        return
    try:
        users.insert_one({
            "login": settings.admin_login,
            "password_hash": password_hasher.hash(settings.admin_password.get_secret_value()),
            "role": "admin",
            "disabled": False,
            "created_at": datetime.now(timezone.utc),
        })
    except DuplicateKeyError:
        # Another worker may have seeded the same login after our initial check.
        pass


def create_token(user_id: str, settings: Settings) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": user_id,
            "iat": now,
            "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )


def set_session(response: Response, token: str, settings: Settings):
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.jwt_expire_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


def unauthorized():
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


def current_user(request: Request) -> dict:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise unauthorized()
    try:
        payload = jwt.decode(
            token,
            request.app.state.settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            issuer=JWT_ISSUER,
            audience=JWT_AUDIENCE,
            options={"require": ["sub", "exp", "iat", "iss", "aud"]},
        )
        user_id = payload["sub"]
        if not isinstance(user_id, str) or not ObjectId.is_valid(user_id):
            raise unauthorized()
    except InvalidTokenError:
        raise unauthorized() from None
    user = request.app.state.database.users.find_one({"_id": ObjectId(user_id)})
    if user is None or user.get("disabled", False):
        raise unauthorized()
    return user


CurrentUser = Annotated[dict, Depends(current_user)]


def admin_user(user: CurrentUser) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required")
    return user


AdminUser = Annotated[dict, Depends(admin_user)]


def authenticate(database, credentials: LoginRequest) -> dict:
    user = database.users.find_one({"login": credentials.login})
    try:
        valid = password_hasher.verify(
            credentials.password, user["password_hash"] if user else DUMMY_HASH
        )
    except (UnknownHashError, ValueError):
        valid = False
    if not valid or user is None or user.get("disabled", False):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid login or password")
    return user
