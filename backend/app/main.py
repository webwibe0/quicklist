from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.ai.paid_limits import budget_message
from app.api.account_recovery import router as recovery_router
from app.api.auth import router as auth_router
from app.api.google_calendar import RedactOAuthQuery
from app.api.google_calendar import router as calendar_router
from app.api.history import router as history_router
from app.api.routes import router
from app.api.voice import router as voice_router
from app.core.config import get_settings
from app.core.spend_budget import BudgetBlocked


@asynccontextmanager
async def lifespan(app):
    from starlette.concurrency import run_in_threadpool

    from app.core.config import get_settings
    from app.db.session import engine
    from app.services.automatic_indexing import AutomaticIndexing
    settings = get_settings()
    worker_settings = settings if settings.embedding_worker_in_web else settings.model_copy(update={'embedding_worker_enabled': False})
    worker=AutomaticIndexing(engine,worker_settings)
    app.state.indexing_worker=worker
    await run_in_threadpool(worker.start)
    try:
        yield
    finally:
        await run_in_threadpool(worker.stop)


app = FastAPI(title="QuickList", version="0.1.0", lifespan=lifespan,
    docs_url='/docs' if get_settings().app_environment == 'local' else None,
    redoc_url=None, openapi_url='/openapi.json' if get_settings().app_environment == 'local' else None)
app.include_router(router)
app.include_router(voice_router)
app.include_router(auth_router)
app.include_router(recovery_router)
app.include_router(history_router)
app.include_router(calendar_router)
app.add_middleware(RedactOAuthQuery)


@app.exception_handler(BudgetBlocked)
async def budget_error(request, exc):
    return JSONResponse(status_code=503, content={'detail': budget_message(exc)})


@app.exception_handler(SQLAlchemyError)
def database_error(request: Request, exc: SQLAlchemyError):
    return JSONResponse(status_code=503, content={
        "detail": "База данных временно недоступна. Попробуйте ещё раз.",
    })


@app.middleware("http")
async def private_responses(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Vary'] = 'Cookie'
    return response


from fastapi.exceptions import RequestValidationError  # noqa: E402


@app.exception_handler(RequestValidationError)
async def validation_error(request, error):
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(item['loc']), "msg": item['msg'], "type": item['type']}
        for item in error.errors()
    ]})
