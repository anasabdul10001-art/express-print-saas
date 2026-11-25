from pydantic_settings import BaseSettings, SettingsConfigDict
class Settings(BaseSettings):
    database_url: str
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24  # 1 day
    # --- eBay OAuth ---
    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    ebay_ru_name: str = ""  # the "RuName" eBay generates for your redirect URL, not a raw URL
    ebay_environment: str = "SANDBOX"  # SANDBOX or PRODUCTION
    # Marketplace Account Deletion notifications. The token and the endpoint
    # must match what's saved on eBay's Alerts & Notifications page exactly,
    # because both are part of the challenge hash.
    ebay_deletion_verification_token: str = ""
    ebay_deletion_endpoint: str = "https://express-print-saas.onrender.com/ebay/account-deletion"
    # Fernet key used to encrypt eBay tokens at rest. Generate with:
    # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    token_encryption_key: str = ""
    # Used to build redirect URLs back to the dashboard after the eBay OAuth callback
    frontend_url: str = "http://localhost:5500"
    # --- Supabase Storage (product image uploads) ---
    supabase_url: str = ""
    supabase_service_key: str = ""
    # --- Resend (transactional email, currently just password-reset links) ---
    resend_api_key: str = ""
    # Must be an address on a domain verified in the Resend dashboard, or
    # Resend's own onboarding@resend.dev sandbox address (which only
    # delivers to the Resend account's own verified email until a real
    # domain is verified).
    resend_from_email: str = "ShipSync <onboarding@resend.dev>"
    # --- Scheduled market-research snapshots ---
    # Shared secret checked on POST /market-research/tracked/run-snapshots,
    # so only our own daily scheduler (not the public internet) can trigger
    # it - it fans out to many eBay API calls and writes to the database.
    cron_secret: str = ""
    # --- DHL Parcel DE Shipping API ---
    # App-level key from developer.dhl.com (one per ShipSync, required on
    # every call regardless of which tenant's own DHL account is billed).
    # Each tenant's own billing_number/username/password (per
    # app/models/dhl_account.py) identifies whose shipping contract
    # actually gets charged - not used yet, label purchase is a later phase.
    dhl_api_key: str = ""
    dhl_environment: str = "SANDBOX"  # SANDBOX or PRODUCTION
    # --- Sentry (error monitoring) ---
    # Empty by default = Sentry stays completely off (see app/core/sentry.py) -
    # this app runs fine, exactly as before, with no DSN configured. Set once
    # a Sentry project exists to start seeing production errors.
    sentry_dsn: str = ""
    sentry_environment: str = "production"
    # 0.0 = no performance/trace sampling overhead, only errors are captured
    # (error capture is NOT gated by this setting - it's always on once a DSN
    # is set). Raise this later if request tracing becomes useful.
    sentry_traces_sample_rate: float = 0.0
    # --- Stripe (subscription billing) ---
    # Empty by default = billing endpoints return a clear "not configured"
    # error instead of crashing - same off-by-default pattern as Sentry.
    # See app/routers/billing.py.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    # --- Shopify OAuth (Dev Dashboard app, authorization-code grant) ---
    shopify_client_id: str = ""
    shopify_client_secret: str = ""
    shopify_redirect_uri: str = "https://express-print-saas.onrender.com/shopify/callback"
    shopify_api_version: str = "2026-07"
    shopify_scopes: str = (
        "read_products,write_products,read_inventory,write_inventory,read_orders,"
        "read_merchant_managed_fulfillment_orders,write_merchant_managed_fulfillment_orders"
    )
    # --- Amazon SP-API (Login with Amazon app; the SP-API needs no AWS signing any more) ---
    amazon_lwa_client_id: str = ""
    amazon_lwa_client_secret: str = ""
    amazon_application_id: str = ""  # the app's id from Seller Central > Develop Apps (consent flow only)
    amazon_redirect_uri: str = "https://express-print-saas.onrender.com/amazon/callback"
    amazon_draft_app: bool = True  # adds version=beta to the consent URL while the app is in Draft
    amazon_seller_central_url: str = "https://sellercentral-europe.amazon.com"
    amazon_sp_api_host: str = "https://sellingpartnerapi-eu.amazon.com"
    amazon_marketplace_id: str = "A1PA6795UKMFR9"  # Amazon.de
    # --- Extra eBay scopes (messages, analytics, account policies, disputes, push) ---
    # Space-separated. Set to an empty value to fall back to the core scopes
    # if eBay rejects the consent screen for one of them.
    ebay_extra_scopes: str = (
        "https://api.ebay.com/oauth/api_scope/sell.account "
        "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly "
        "https://api.ebay.com/oauth/api_scope/commerce.message "
        "https://api.ebay.com/oauth/api_scope/sell.payment.dispute "
        "https://api.ebay.com/oauth/api_scope/commerce.notification.subscription"
    )
    # --- AI reply suggestions (free tiers; first configured key is tried first) ---
    groq_api_key: str = ""
    gemini_api_key: str = ""
    openrouter_api_key: str = ""
    nvidia_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    gemini_model: str = "gemini-3.5-flash-lite"
    openrouter_model: str = "meta-llama/llama-3.3-70b-instruct:free"
    nvidia_model: str = "meta/llama-3.3-70b-instruct"
    # --- Reading supplier invoices (free tiers, tried in this order: Groq, Mistral,
    # OpenRouter; Groq and OpenRouter use the keys above) ---
    mistral_api_key: str = ""
    groq_vision_model: str = "qwen/qwen3.8-27b"
    mistral_vision_model: str = "mistral-small-latest"
    openrouter_vision_model: str = "google/gemma-4-31b-it:free"
    # Background order sync every N minutes (0 = off). The orders page also
    # syncs by itself while it is open.
    auto_sync_minutes: int = 0
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)
settings = Settings()