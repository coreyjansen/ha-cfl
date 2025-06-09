""" CFL Team Status """
import asyncio
import logging
from datetime import timedelta
import arrow

import aiohttp
from async_timeout import timeout

STATUS_MAP = {
    "scheduled": "PRE",
    "live": "IN",
    "inprogress": "IN",
    "complete": "POST",
    "final": "POST",
}
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_registry import (
    async_entries_for_config_entry,
    async_get,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_ENDPOINT,
    CONF_TIMEOUT,
    CONF_TEAM_ID,
    COORDINATOR,
    DEFAULT_TIMEOUT,
    DOMAIN,
    ISSUE_URL,
    PLATFORMS,
    USER_AGENT,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load the saved entities."""
    # Print startup message
    _LOGGER.info(
        "CFL version %s is starting, if you have any issues please report them here: %s",
        VERSION,
        ISSUE_URL,
    )
    hass.data.setdefault(DOMAIN, {})

    entry.add_update_listener(update_listener)

    if entry.unique_id is not None:
        hass.config_entries.async_update_entry(entry, unique_id=None)

        ent_reg = async_get(hass)
        for entity in async_entries_for_config_entry(ent_reg, entry.entry_id):
            ent_reg.async_update_entity(entity.entity_id, new_unique_id=entry.entry_id)

    # Setup the data coordinator
    coordinator = CFLDataUpdateCoordinator(
        hass, entry.data, entry.data.get(CONF_TIMEOUT)
    )

    # Fetch initial data so we have data when entities subscribe
    await coordinator.async_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        COORDINATOR: coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""

    _LOGGER.debug("Attempting to unload entities from the %s integration", DOMAIN)

    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(config_entry, platform)
                for platform in PLATFORMS
            ]
        )
    )

    if unload_ok:
        _LOGGER.debug("Successfully removed entities from the %s integration", DOMAIN)
        hass.data[DOMAIN].pop(config_entry.entry_id)

    return unload_ok


async def update_listener(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Update listener."""

    _LOGGER.debug("Attempting to reload entities from the %s integration", DOMAIN)

    if config_entry.data == config_entry.options:
        _LOGGER.debug("No changes detected not reloading entities.")
        return

    new_data = config_entry.options.copy()

    hass.config_entries.async_update_entry(
        entry=config_entry,
        data=new_data,
    )

    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_migrate_entry(hass, config_entry):
    """Migrate an old config entry."""
    version = config_entry.version

    # 1-> 2: Migration format
    if version == 1:
        _LOGGER.debug("Migrating from version %s", version)
        updated_config = config_entry.data.copy()

        if CONF_TIMEOUT not in updated_config.keys():
            updated_config[CONF_TIMEOUT] = DEFAULT_TIMEOUT

        if updated_config != config_entry.data:
            hass.config_entries.async_update_entry(config_entry, data=updated_config)

        config_entry.version = 2
        _LOGGER.debug("Migration to version %s complete", config_entry.version)

    return True


class CFLDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching CFL data."""

    def __init__(self, hass, config, the_timeout: int):
        """Initialize."""
        self.interval = timedelta(minutes=10)
        self.name = config[CONF_NAME]
        self.timeout = the_timeout
        self.config = config
        self.hass = hass

        _LOGGER.debug("Data will be updated every %s", self.interval)

        super().__init__(hass, _LOGGER, name=self.name, update_interval=self.interval)

    async def _async_update_data(self):
        """Fetch data"""
        async with timeout(self.timeout):
            try:
                data = await update_game(self.config)
                # update the interval based on flag
                if data["private_fast_refresh"] == True:
                    self.update_interval = timedelta(seconds=5)
                else:
                    self.update_interval = timedelta(minutes=10)
            except Exception as error:
                raise UpdateFailed(error) from error
            return data


async def update_game(config) -> dict:
    """Fetch new state data for the sensor.
    This is the only method that should fetch new data for Home Assistant.
    """

    data = await async_get_state(config)
    return data


async def async_get_state(config) -> dict:
    """Get CFL state and flatten into CFL-style dict expected by sensor."""
    team_id = config[CONF_TEAM_ID].upper()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    async with aiohttp.ClientSession() as session:
        async with session.get(API_ENDPOINT, headers=headers) as resp:
            rounds = await resp.json(content_type=None)

    # locate the first game that involves the requested team
    game = None
    for rnd in rounds:
        for g in rnd.get("tournaments", []):
            if team_id in (
                g["homeSquad"]["shortName"].upper(),
                g["awaySquad"]["shortName"].upper(),
            ):
                game = g
                break
        if game:
            break

    # if nothing found, reuse existing helper to clear attributes
    if game is None:
        values = await async_clear_states(config)
        values["state"] = "NOT_FOUND"
        values["last_update"] = arrow.now().format(arrow.FORMAT_W3C)
        return values

    # helper to translate home/away blocks
    def _side(which):
        sq = game[f"{which}Squad"]
        return {
            "abbr": sq["shortName"],
            "id": sq["id"],
            "name": sq["name"],
            "score": sq["score"],
            "timeouts": game["timeouts"][which],
            "homeaway": which,
        }

    home = _side("home")
    away = _side("away")
    team, opp = (home, away) if home["abbr"].upper() == team_id else (away, home)

    values = {
        # basic / generic -------------------------------------------------
        "state": STATUS_MAP.get(game["status"].lower(), game["status"].upper()),
        "date": game["date"],
        "kickoff_in": arrow.get(game["date"]).humanize(),
        "quarter": game.get("activePeriod"),
        "clock": game.get("clock"),
        "venue": None,
        "location": None,
        "tv_network": None,
        "odds": game.get("markets"),
        "overunder": None,
        "possession": game.get("possession"),
        "last_play": None,
        "down_distance_text": None,
        # team ------------------------------------------------------------
        "team_abbr": team["abbr"],
        "team_id": team["id"],
        "team_name": team["name"],
        "team_record": None,
        "team_homeaway": team["homeaway"],
        "team_logo": None,
        "team_colors": None,
        "team_score": team["score"],
        "team_win_probability": None,
        "team_timeouts": team["timeouts"],
        # opponent --------------------------------------------------------
        "opponent_abbr": opp["abbr"],
        "opponent_id": opp["id"],
        "opponent_name": opp["name"],
        "opponent_record": None,
        "opponent_homeaway": opp["homeaway"],
        "opponent_logo": None,
        "opponent_colors": None,
        "opponent_score": opp["score"],
        "opponent_win_probability": None,
        "opponent_timeouts": opp["timeouts"],
        # misc ------------------------------------------------------------
        "last_update": arrow.now().format(arrow.FORMAT_W3C),
        "private_fast_refresh": (
            (
                (STATUS_MAP.get(game["status"].lower()) in ["PRE", "IN"])
                and (arrow.get(game["date"]) - arrow.now()).total_seconds() < 1200
            )
        ),
    }

    return values


async def async_clear_states(config) -> dict:
    """Clear all state attributes"""

    values = {}
    # Reset values
    values = {
        "date": None,
        "kickoff_in": None,
        "quarter": None,
        "clock": None,
        "venue": None,
        "location": None,
        "tv_network": None,
        "odds": None,
        "overunder": None,
        "last_play": None,
        "down_distance_text": None,
        "possession": None,
        "team_id": None,
        "team_record": None,
        "team_homeaway": None,
        "team_colors": None,
        "team_score": None,
        "team_win_probability": None,
        "team_timeouts": None,
        "opponent_abbr": None,
        "opponent_id": None,
        "opponent_name": None,
        "opponent_record": None,
        "opponent_homeaway": None,
        "opponent_logo": None,
        "opponent_colors": None,
        "opponent_score": None,
        "opponent_win_probability": None,
        "opponent_timeouts": None,
        "last_update": None,
        "private_fast_refresh": False,
    }

    return values
