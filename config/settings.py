import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-secret-key-not-for-production")
DEBUG = os.environ.get("DJANGO_DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.staticfiles",
    "rest_framework",
    "fuel",
    "routes",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# DATABASE_URL unset -> local sqlite (used for quick/offline test runs).
# docker-compose provides DATABASE_URL pointing at the postgres service.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}

# --- Vehicle / domain configuration (never hardcoded in application code) ---
VEHICLE_MPG = float(os.environ.get("VEHICLE_MPG", "10"))
VEHICLE_MAX_RANGE_MILES = float(os.environ.get("VEHICLE_MAX_RANGE_MILES", "500"))
# Defaults to max_range/mpg (the documented assumption) but is independently
# configurable rather than hardcoded, in case that ever needs to change.
VEHICLE_TANK_CAPACITY_GALLONS = float(
    os.environ.get("VEHICLE_TANK_CAPACITY_GALLONS", str(VEHICLE_MAX_RANGE_MILES / VEHICLE_MPG))
)
FUEL_STATION_ROUTE_BUFFER_MILES = float(
    os.environ.get("FUEL_STATION_ROUTE_BUFFER_MILES", "5")
)

OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
OSRM_ROUTING_PROFILE = os.environ.get("OSRM_ROUTING_PROFILE", "driving")
NOMINATIM_BASE_URL = os.environ.get(
    "NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org"
)
NOMINATIM_USER_AGENT = os.environ.get(
    "NOMINATIM_USER_AGENT", "fuel-route-optimizer-assessment/1.0"
)

ROUTE_CACHE_SCHEMA_VERSION = "v1"
GEOCODE_CACHE_SCHEMA_VERSION = "v1"

# Projected CRS used by the geospatial service for station-to-route calculations.
# EPSG:5070 (NAD83 / Conus Albers) — a practical CONUS-appropriate equal-area
# projection for this assessment's USA-only scope, not a universally exact
# distance projection. See routes/services/geospatial.py.
GEOSPATIAL_PROJECTED_CRS = "EPSG:5070"
