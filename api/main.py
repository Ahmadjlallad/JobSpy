from __future__ import annotations

import logging
import math
import os
from datetime import date, datetime
from typing import Annotated, Literal

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException, status
from jobspy import scrape_jobs
from pydantic import BaseModel, ConfigDict, Field

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("jobspy.api")

API_TOKEN = os.environ.get("SCRAPER_TOKEN", "")

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
    if authorization != f"Bearer {API_TOKEN}":
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


@app.post(
    "/scrape",
    response_model=ScrapeResponse,
    dependencies=[Depends(require_token)],
)
def scrape(payload: ScrapeRequest) -> ScrapeResponse:
    log.info(
        "scrape request: sites=%s term=%r location=%r country=%r country_indeed=%r results=%d",
        payload.site_names,
        payload.search_term,
        payload.location,
        payload.country,
        payload.country_indeed,
        payload.results_wanted,
    )
    try:
        df = scrape_jobs(
            site_name=list(payload.site_names),
            search_term=payload.search_term,
            google_search_term=payload.google_search_term,
            location=payload.location,
            distance=payload.distance,
            is_remote=payload.is_remote,
            job_type=payload.job_type,
            results_wanted=payload.results_wanted,
            country=payload.country,
            country_indeed=payload.country_indeed,
            hours_old=payload.hours_old,
            description_format=payload.description_format,
            linkedin_fetch_description=payload.linkedin_fetch_description,
            verbose=1,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except Exception as exc:
        log.exception("scrape failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    jobs = _df_to_jobs(df)
    return ScrapeResponse(count=len(jobs), jobs=jobs)
