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

    # Fernet key used to encrypt eBay tokens at rest. Generate with:
    # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    token_encryption_key: str = ""

    # Used to build redirect URLs back to the dashboard after the eBay OAuth callback
    frontend_url: str = "http://localhost:5500"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)


settings = Settings()
