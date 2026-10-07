"""Forecast sources.  Each module offers one Provider; ``build_providers`` picks them by name.

A provider talks to one service with the standard library, sends the site's rounded
coordinates and nothing else, gives up within its own timeout, and raises ProviderError
with a message that carries no key or token.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import ForecastConfig
    from ..forecast import Provider

KNOWN = ("open-meteo", "7timer", "caiyun", "qweather")
_LOG = logging.getLogger(__name__)


def build_providers(config: ForecastConfig) -> list[Provider]:
    """The configured sources, in order; a source whose key is missing or whose adapter is absent is left out."""

    providers: list[Provider] = []
    for name in config.providers:
        try:
            if name == "open-meteo":
                from .openmeteo import OpenMeteo

                providers.append(OpenMeteo(models=config.open_meteo_models))
            elif name == "7timer":
                from .seventimer import SevenTimer

                providers.append(SevenTimer())
            elif name == "caiyun" and config.caiyun_token:
                from .caiyun import Caiyun

                providers.append(Caiyun(config.caiyun_token))
            elif name == "qweather" and config.qweather_key:
                from .qweather import QWeather

                providers.append(QWeather(config.qweather_key, host=config.qweather_host))
        except ImportError as error:
            _LOG.warning("forecast source %s is not available: %s", name, error)
    return providers
