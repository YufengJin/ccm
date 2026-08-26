from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Profile:
    name: str
    path: Path
    compat_link: Optional[Path] = None
    account_uuid: Optional[str] = None
    email: Optional[str] = None
    subscription: Optional[str] = None
    rate_limit_tier: Optional[str] = None
    identity_fetched_at: Optional[int] = None
    note: str = ""
