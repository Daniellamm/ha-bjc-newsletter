"""Constants for the BJC Newsletter integration."""

DOMAIN = "bjc_newsletter"
NAME = "BJC Newsletter"

# Config entry data keys
CONF_GEMINI_API_KEY = "gemini_api_key"
CONF_GEMINI_MODEL = "gemini_model"

# Options keys (persisted state)
OPT_CURRENT_NEWSLETTER_URL = "current_newsletter_url"
OPT_LAST_PROCESSED = "last_processed"
OPT_LAST_CHECKED = "last_checked"
OPT_LAST_ATTEMPTED_URL = "last_attempted_url"  # set even on Gemini failure to prevent retry storm

# Polling
UPDATE_INTERVAL_HOURS = 1

# BJC sources
BJC_HOMEPAGE_URL = "https://www.bocajewishcenter.org/"
BJC_FLIPSNACK_ACCOUNT = "7BBDB688B7A"
FLIPSNACK_PDF_PATTERN = "https://www.flipsnack.com/{account}/{slug}.pdf"
FLIPSNACK_FULLVIEW_PATTERN = "https://www.flipsnack.com/{account}/{slug}/full-view.html"

# Gemini defaults
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# Cache file
CACHE_FILENAME = "bjc_newsletter_cache.json"

# PDF watch folder — place newsletter PDFs here for automatic processing.
# Path is relative to the HA config directory (e.g. /config/bjc_newsletter_pdfs/).
# The integration picks up the newest .pdf file in this folder that is newer
# than the last successfully processed newsletter.
PDF_WATCH_FOLDER = "bjc_newsletter_pdfs"

# Sensor keys
SENSOR_TODAY = "today_schedule"
SENSOR_TOMORROW = "tomorrow_schedule"
SENSOR_STATUS = "newsletter_status"

# Status values
STATUS_READY = "ready"
STATUS_PROCESSING = "processing"
STATUS_ERROR = "error"
STATUS_IDLE = "idle"

# Coordinator data keys
DATA_SCHEDULE = "schedule"       # dict[str, str]: date_isoformat -> markdown
DATA_STATUS = "status"           # one of STATUS_* constants
DATA_NEWSLETTER_URL = "newsletter_url"
DATA_LAST_PROCESSED = "last_processed"
DATA_LAST_CHECKED = "last_checked"
DATA_LAST_ERROR = "last_error"

# PDF compression
PDF_TEXT_MIN_CHARS = 500         # Min chars to consider text extraction usable
PDF_IMAGE_QUALITY = 40           # JPEG quality — no resize, just recompress (43MB → ~4-5MB)
