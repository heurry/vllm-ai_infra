"""Application configuration."""

from pathlib import Path

from pydantic import BaseModel


class Settings(BaseModel):
    """Static settings for the initial project scaffold."""

    app_name: str = "diagnostic-llm-platform"
    api_prefix: str = "/api/v1"
    mineru_root: Path = Path("/home/xdu/MinerU")
    default_model_name: str = "Qwen2.5-7B-Instruct"
    default_vllm_base_url: str = "http://127.0.0.1:8000/v1"


settings = Settings()

