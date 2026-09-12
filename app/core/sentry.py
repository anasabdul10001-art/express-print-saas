"""
Sentry error monitoring - off by default (see app/config.py: settings.sentry_dsn
defaults to an empty string), so the app behaves exactly as it does today
until a real Sentry DSN is configured. Call init_sentry() once, before the
FastAPI app is created (see app/main.py) - sentry_sdk's FastAPI/Starlette
integrations are auto-detected once sentry_sdk.init() has run, no explicit
middleware wiring needed on the app itself.
"""

import sentry_sdk

from app.config import settings


def init_sentry() -> None:
    if not settings.sentry_dsn:
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
    )
