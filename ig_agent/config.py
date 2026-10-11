"""Settings loaded from environment variables (or a local .env file)."""

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, existing env vars win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    ig_user_id: str
    access_token: str
    graph_version: str = "v23.0"
    app_secret: str = ""
    webhook_verify_token: str = ""
    data_dir: Path = Path("data")

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        missing = [k for k in ("IG_USER_ID", "IG_ACCESS_TOKEN") if not os.environ.get(k)]
        if missing:
            raise SystemExit(
                f"Missing required environment variables: {', '.join(missing)}. "
                "See .env.example."
            )
        data_dir = Path(os.environ.get("IG_DATA_DIR", "data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            ig_user_id=os.environ["IG_USER_ID"],
            access_token=os.environ["IG_ACCESS_TOKEN"],
            graph_version=os.environ.get("IG_GRAPH_VERSION", "v23.0"),
            app_secret=os.environ.get("IG_APP_SECRET", ""),
            webhook_verify_token=os.environ.get("IG_WEBHOOK_VERIFY_TOKEN", ""),
            data_dir=data_dir,
        )
