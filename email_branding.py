"""FundraiseUp email branding defaults.

Images must be publicly reachable HTTPS PNG/JPEG — Gmail blocks AVIF/WebP and
relative paths. Banner/logo are hosted on Supabase public storage.
"""

# Platform F mark (PNG) for email headers.
DEFAULT_EMAIL_LOGO_URL = (
    "https://galiikzdbkbtqkhxlkgy.supabase.co/storage/v1/object/public/"
    "campaign-assets/campaigns/a8312bd1-f9b9-4ec1-8d28-ddb28efd9bb5.png"
)

# Soft watercolor banner (compressed JPEG) for email headers.
DEFAULT_EMAIL_BANNER_URL = (
    "https://galiikzdbkbtqkhxlkgy.supabase.co/storage/v1/object/public/"
    "campaign-assets/campaigns/8bd051d2-f10a-4eec-8721-e7dc06af43b9.jpg"
)

DEFAULT_BRAND_NAME = "FundraiseUp"
DEFAULT_PRIMARY_COLOR = "#3872DC"
