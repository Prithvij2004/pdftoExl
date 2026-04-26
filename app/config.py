from __future__ import annotations

from dotenv import load_dotenv
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


# Ensure arbitrary .env vars (like AWS_ACCESS_KEY_ID) are available to boto3.
# BaseSettings reads .env into Settings fields, but boto3 relies on process env vars.
load_dotenv(".env", override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    aws_region: str = "us-east-1"
    # Prefer inference-profile ID for Nova (avoids on-demand unsupported errors).
    bedrock_model_id: str = "us.amazon.nova-pro-v1:0"

    pdf_batch_size: int = 4
    max_upload_mb: int = 25
    debug_json: bool = False

    runtime_dir: Path = Path("runtime")
    uploads_dir: Path = Path("runtime/uploads")
    generated_dir: Path = Path("runtime/generated")


settings = Settings()
