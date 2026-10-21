"""Shared application context (one per app instance)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import releaselock
from .access import RemoteAccess
from .auth import Auth
from .calibrate import Calibrator
from .jobs import JobManager
from .moonraker import Moonraker
from .network import Network
from .services import Services
from .settings import Settings
from .state import StateStore


@dataclass
class Context:
    settings: Settings
    state: StateStore
    jobs: JobManager = field(default_factory=JobManager)
    services: Services = None            # type: ignore[assignment]
    network: Network = None              # type: ignore[assignment]
    moonraker: Moonraker = None          # type: ignore[assignment]
    calibrator: Calibrator = None        # type: ignore[assignment]
    lock: dict[str, Any] = field(default_factory=dict)
    auth: Auth = None                    # type: ignore[assignment]
    access: RemoteAccess = None          # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.auth = Auth(self.settings, self.state)
        self.services = Services(self.settings)
        self.access = RemoteAccess(self.settings)
        self.network = Network(self.settings)
        self.moonraker = Moonraker(self.settings.moonraker_url)
        self.calibrator = Calibrator(self.moonraker)
        self.lock = releaselock.load(self.settings.release_lock)
