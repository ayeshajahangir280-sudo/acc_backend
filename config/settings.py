"""Django settings. All environment-specific values come from environment variables."""
import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_list(name, default=""):
    return [v.strip() for v in os.environ.get(name, default).split(",") if v.strip()]


def env_origins(*names):
    # First non-empty variable wins. Origins must be scheme://host[:port] with no
    # path, so tolerate a trailing "/".
    for name in names:
        if values := env_list(name):
            return [v.rstrip("/") for v in values]
    return []


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "dev-only-insecure-key"
    else:
        raise RuntimeError("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG is not true.")

# Host filtering is left to the reverse proxy (Traefik); override to restrict.
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "*")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "rest_framework.authtoken",
    "django_filters",
    "apps.core",
    "apps.organizations",
    "apps.parties",
    "apps.ledger",
    "apps.sales",
    "apps.purchases",
    "apps.treasury",
    "apps.vouchers",
    "apps.expenses",
    "apps.banking",
    "apps.reports",
    "apps.inventory",
    "apps.dispatch",
    "apps.taxes",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

# Database: engine and credentials are fully driven by DATABASE_URL
# (e.g. postgres://user:pass@host:5432/dbname). The SQLite fallback exists only
# for local development/tests and is never used when DATABASE_URL is set.
_db_url = os.environ.get("DATABASE_URL")
DATABASES = {
    "default": dj_database_url.parse(_db_url, conn_max_age=int(os.environ.get("DB_CONN_MAX_AGE", "60")))
    if _db_url
    else {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "dev.sqlite3"}
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Dubai"  # fixed for the UAE; intentionally not configurable via environment
USE_I18N = True
USE_TZ = True

MEDIA_ROOT = Path(os.environ.get("DJANGO_MEDIA_ROOT", str(BASE_DIR / "media")))
MEDIA_URL = "media/"  # files are served only through authenticated API endpoints, never directly
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.TokenAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.StandardPagination",
    "PAGE_SIZE": 50,
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.UserRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"user": os.environ.get("API_THROTTLE_USER", "2000/hour"), "login": "20/min"},
}

# CORS_ORIGIN: comma-separated frontend origins (CORS_ALLOWED_ORIGINS also accepted).
CORS_ALLOWED_ORIGINS = env_origins("CORS_ORIGIN", "CORS_ALLOWED_ORIGINS")
CORS_ALLOW_HEADERS = [
    "accept", "authorization", "content-type", "origin", "x-organization-id", "x-requested-with",
]
CSRF_TRUSTED_ORIGINS = env_origins("CSRF_TRUSTED_ORIGINS") or CORS_ALLOWED_ORIGINS

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # Traefik terminates HTTPS and handles redirects; opt in only if needed.
    SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SSL_REDIRECT", "false").lower() == "true"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "0"))
