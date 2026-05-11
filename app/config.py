from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict


load_dotenv(".env", override=False, encoding="utf-8-sig")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8-sig", extra="ignore")

    pdf_parser: str = "docling"
    output_dir: Path = Path("outputs")
    llm_provider: str = "bedrock"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 8192
    aws_region: str = "us-east-1"
    bedrock_model_id: str = "us.amazon.nova-pro-v1:0"
    max_upload_mb: int = 25

    runtime_dir: Path = Path("runtime")
    uploads_dir: Path = Path("runtime/uploads")
    generated_dir: Path = Path("runtime/generated")
    downloads_dir: Path = Path.home() / "Downloads"


settings = Settings()
