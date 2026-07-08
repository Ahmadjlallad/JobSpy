from __future__ import annotations

import asyncio
import functools
import logging
import math
import os
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Annotated, Literal

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, status
from jobspy import scrape_jobs
from pydantic import BaseModel, ConfigDict, Field

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("jobspy.api")

API_TOKEN = os.environ.get("SCRAPER_TOKEN", "")
SCRAPER_ENV = os.environ.get("SCRAPER_ENV", "local")

# Cap simultaneous scrapes: parallel same-IP hits to LinkedIn/Indeed are the
# fastest route to an IP ban, and each scrape is memory/thread heavy.
MAX_CONCURRENCY = max(1, int(os.environ.get("SCRAPER_MAX_CONCURRENCY", "3")))
# Hard ceiling per scrape so a hung upstream fetch cannot pin a worker forever.
SCRAPE_TIMEOUT = max(1, int(os.environ.get("SCRAPER_SCRAPE_TIMEOUT", "300")))

# Fail closed: refuse to boot an unauthenticated (or default-token) service in
# production instead of silently exposing an open /scrape endpoint.
if SCRAPER_ENV == "production" and (not API_TOKEN or API_TOKEN == "change-me"):
    raise RuntimeError(
        "SCRAPER_TOKEN must be set to a strong value when SCRAPER_ENV=production."
    )

if not API_TOKEN:
    log.warning(
        "SCRAPER_TOKEN is not set: the /scrape endpoint is UNAUTHENTICATED. "
        "Set SCRAPER_TOKEN before exposing this service beyond localhost."
    )

# One executor sized to the concurrency cap, plus a semaphore that admits at
# most that many in-flight scrapes and rejects the overflow with 503 rather
# than queueing unbounded work.
_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENCY)
_semaphore = threading.BoundedSemaphore(MAX_CONCURRENCY)

app = FastAPI(title="jobspy api", version="0.1.0")


SiteName = Literal[
    "linkedin",
    "indeed",
    "zip_recruiter",
    "glassdoor",
    "google",
    "bayt",
    "naukri",
    "bdjobs",
]
JobTypeName = Literal["fulltime", "parttime", "internship", "contract", "temporary"]
DescriptionFormatName = Literal["markdown", "html", "plain"]


def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    if not API_TOKEN:
        return
    expected = f"Bearer {API_TOKEN}"
    # Constant-time comparison so a caller cannot recover the token by
    # measuring response timing.
    if authorization is None or not secrets.compare_digest(authorization, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
        )


class ScrapeRequest(BaseModel):
    site_names: list[SiteName] = Field(
        default_factory=lambda: ["linkedin", "indeed", "google", "bayt"]
    )
    search_term: str | None = None
    google_search_term: str | None = None
    location: str | None = None
    country: str | None = None
    country_indeed: str = "usa"
    distance: int = Field(default=50, ge=0, le=500)
    is_remote: bool = False
    job_type: JobTypeName | None = None
    results_wanted: int = Field(default=20, ge=1, le=1000)
    hours_old: int | None = Field(default=None, ge=1)
    description_format: DescriptionFormatName = "markdown"
    linkedin_fetch_description: bool = False
    easy_apply: bool | None = None
    proxies: list[str] | str | None = None
    ca_cert: str | None = None
    offset: int | None = 0
    enforce_annual_salary: bool = False
    linkedin_company_ids: list[int] | None = None
    user_agent: str | None = None


class JobRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    site: str
    job_url: str
    job_url_direct: str | None = None
    title: str
    company: str | None = None
    location: str | None = None
    date_posted: date | None = None
    job_type: str | None = None
    salary_source: str | None = None
    interval: str | None = None
    min_amount: float | None = None
    max_amount: float | None = None
    currency: str | None = None
    is_remote: bool | None = None
    job_level: str | None = None
    job_function: str | None = None
    listing_type: str | None = None
    emails: str | None = None
    description: str | None = None
    company_industry: str | None = None
    company_url: str | None = None
    company_logo: str | None = None
    company_url_direct: str | None = None
    company_addresses: str | None = None
    company_num_employees: str | None = None
    company_revenue: str | None = None
    company_description: str | None = None
    skills: list[str] | None = None
    experience_range: str | None = None
    company_rating: float | None = None
    company_reviews_count: int | None = None
    vacancy_count: int | None = None
    work_from_home_type: str | None = None


class ScrapeResponse(BaseModel):
    count: int
    jobs: list[JobRecord]


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if value is pd.NaT:
        return True
    return False


def _normalize(value: object) -> object:
    if _is_missing(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime().date()
    if isinstance(value, datetime):
        return value.date()
    return value


def _df_to_jobs(df: pd.DataFrame | None) -> list[JobRecord]:
    if df is None or df.empty:
        return []
    jobs: list[JobRecord] = []
    for raw_row in df.to_dict(orient="records"):
        cleaned = {key: _normalize(value) for key, value in raw_row.items()}
        jobs.append(JobRecord.model_validate(cleaned))
    return jobs


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _run_scrape(payload: ScrapeRequest) -> pd.DataFrame:
    """Blocking scrape; runs inside the bounded executor."""
    return scrape_jobs(
        site_name=list(payload.site_names),
        search_term=payload.search_term,
        google_search_term=payload.google_search_term,
        location=payload.location,
        distance=payload.distance,
        is_remote=payload.is_remote,
        job_type=payload.job_type,
        easy_apply=payload.easy_apply,
        results_wanted=payload.results_wanted,
        country=payload.country,
        country_indeed=payload.country_indeed,
        hours_old=payload.hours_old,
        description_format=payload.description_format,
        linkedin_fetch_description=payload.linkedin_fetch_description,
        proxies=payload.proxies,
        ca_cert=payload.ca_cert,
        offset=payload.offset,
        enforce_annual_salary=payload.enforce_annual_salary,
        linkedin_company_ids=payload.linkedin_company_ids,
        user_agent=payload.user_agent,
        verbose=1,
    )


@app.post(
    "/scrape",
    response_model=ScrapeResponse,
    dependencies=[Depends(require_token)],
)
async def scrape(payload: ScrapeRequest) -> ScrapeResponse:
    log.info(
        "scrape request: sites=%s term=%r location=%r country=%r country_indeed=%r results=%d",
        payload.site_names,
        payload.search_term,
        payload.location,
        payload.country,
        payload.country_indeed,
        payload.results_wanted,
    )

    # Admit at most MAX_CONCURRENCY scrapes; shed load instead of queueing.
    if not _semaphore.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="scraper is at capacity, retry shortly",
        )

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_executor, functools.partial(_run_scrape, payload))
    released = False
    try:
        # shield keeps the executor task running to completion even when the
        # timeout cancels our await, so the concurrency slot is only released
        # once the (uninterruptible) scrape thread actually finishes.
        df = await asyncio.wait_for(asyncio.shield(future), timeout=SCRAPE_TIMEOUT)
        _semaphore.release()
        released = True
    except asyncio.TimeoutError as exc:
        future.add_done_callback(lambda _: _semaphore.release())
        released = True
        log.warning("scrape timed out after %ss", SCRAPE_TIMEOUT)
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="scrape timed out",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except Exception as exc:
        # Do not leak internal exception text (may contain proxy creds/paths).
        log.exception("scrape failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="upstream scrape failed",
        ) from exc
    finally:
        if not released:
            _semaphore.release()

    jobs = _df_to_jobs(df)
    return ScrapeResponse(count=len(jobs), jobs=jobs)
