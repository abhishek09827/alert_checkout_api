"""Entrypoint: `python main.py` or `uvicorn app.main:app`."""
import uvicorn

from app.core.config import get_settings

settings = get_settings()

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        # structlog owns logging; uvicorn must not configure its own handlers.
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=30,
    )
