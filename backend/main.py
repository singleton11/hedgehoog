from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pymongo import MongoClient
from pymongo.errors import PyMongoError
from starlette.concurrency import run_in_threadpool

from auth import (
    COOKIE_NAME,
    AdminUser,
    CurrentUser,
    LoginRequest,
    UserResponse,
    authenticate,
    create_token,
    public_user,
    seed_admin,
    set_session,
)
from calibration_results import ensure_indexes as ensure_calibration_indexes
from celery_app import enqueue_etf_import
from config import Settings
from customer_planner import ensure_indexes as ensure_customer_planner_indexes, router as customer_planner_router
from instrument_routes import router as instrument_router
from index_routes import router as index_router
from indices import ensure_catalogue as ensure_index_catalogue
from goal_planner import router as goal_router
from market_data import DataError
from manual_splits import ensure_indexes as ensure_manual_split_indexes
from portfolio_tracker import ensure_indexes as ensure_portfolio_indexes, router as portfolio_router
from degiro_import import router as degiro_import_router


def create_app(settings: Settings | None = None, client_factory=MongoClient) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings or Settings()
        client = client_factory(config.mongodb_uri.get_secret_value(), serverSelectionTimeoutMS=5000)
        app.state.settings = config
        app.state.database = client[config.mongodb_database]
        try:
            await run_in_threadpool(client.admin.command, "ping")
            await run_in_threadpool(seed_admin, app.state.database, config)
            await run_in_threadpool(ensure_calibration_indexes, app.state.database)
            await run_in_threadpool(ensure_customer_planner_indexes, app.state.database)
            await run_in_threadpool(ensure_portfolio_indexes, app.state.database)
            await run_in_threadpool(ensure_manual_split_indexes, app.state.database)
            await run_in_threadpool(ensure_index_catalogue, app.state.database)
            await run_in_threadpool(enqueue_etf_import, config)
            yield
        finally:
            client.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(instrument_router)
    app.include_router(index_router)
    app.include_router(goal_router)
    app.include_router(customer_planner_router)
    app.include_router(portfolio_router)
    app.include_router(degiro_import_router)

    @app.exception_handler(DataError)
    async def acquisition_error(request: Request, exc: DataError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.middleware("http")
    async def security_headers_and_origin_check(request: Request, call_next):
        # Cookies are HttpOnly + SameSite=Strict; also reject cross-origin mutations,
        # including login/logout. Non-browser API clients may omit Origin.
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            same_origin = f"{request.url.scheme}://{request.url.netloc}"
            allowed_origins = request.app.state.settings.allowed_origins
            if (
                origin is not None and origin != same_origin and origin not in allowed_origins
            ) or request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(PyMongoError)
    async def database_unavailable(request: Request, exc: PyMongoError):
        return JSONResponse({"detail": "Database unavailable"}, status_code=503)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/auth/login", response_model=UserResponse)
    def login(credentials: LoginRequest, request: Request, response: Response):
        user = authenticate(request.app.state.database, credentials)
        set_session(response, create_token(str(user["_id"]), request.app.state.settings), request.app.state.settings)
        return public_user(user)

    @app.get("/auth/me", response_model=UserResponse)
    def me(user: CurrentUser):
        return public_user(user)

    @app.post("/auth/logout", status_code=204)
    def logout(request: Request, response: Response):
        response.delete_cookie(
            COOKIE_NAME, path="/", httponly=True,
            secure=request.app.state.settings.cookie_secure, samesite="strict",
        )

    @app.get("/")
    def read_root(user: CurrentUser):
        return {"message": "Hello, World!"}

    @app.get("/admin")
    def read_admin(user: AdminUser):
        return {"message": "Administrator access granted"}

    return app


app = create_app()
