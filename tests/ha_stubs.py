"""Shared stand-ins for Home Assistant and aiohttp, so tests run without either.

Every test module imports this first. The stubs are installed once per process,
so the test modules no longer depend on the order in which they are loaded.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "custom_components" / "weishaupt_wtc"
PACKAGE = "custom_components.weishaupt_wtc"


def _module(name: str, package: bool = False, **attrs) -> ModuleType:
    module = ModuleType(name)
    if package:
        module.__path__ = []
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class _NameEnum:
    """Return the requested member name as a string, like a StrEnum value."""

    def __getattr__(self, name: str) -> str:
        if name.startswith("__"):
            raise AttributeError(name)
        return name.lower()


class _Generic:
    """Base for stub classes that are subscripted in annotations (Foo[Bar])."""

    def __class_getitem__(cls, item):
        return cls


# --- aiohttp -----------------------------------------------------------------


class ClientError(Exception):
    """aiohttp.ClientError stub."""


class ClientConnectorError(ClientError):
    """aiohttp.ClientConnectorError stub."""


class BasicAuth:
    def __init__(self, login: str, password: str) -> None:
        self.login = login
        self.password = password


class ClientTimeout:
    def __init__(self, total: int | None = None) -> None:
        self.total = total


class ClientSession:
    closed = False

    async def close(self) -> None:
        """Interface used by the client close method."""


# --- homeassistant.helpers.update_coordinator --------------------------------


class UpdateFailed(Exception):
    """Coordinator update failure."""


class DataUpdateCoordinator(_Generic):
    """Keeps what the integration uses: data, update_interval and listener contexts."""

    def __init__(self, hass, logger, *, name, update_interval=None, config_entry=None) -> None:
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.config_entry = config_entry
        self.data = None
        self.last_update_success = True
        self.last_exception = None
        self._contexts: list = []

    def async_contexts(self):
        # Like Home Assistant: listeners registered without a context are skipped.
        return (context for context in self._contexts if context is not None)


class CoordinatorEntity(_Generic):
    def __init__(self, coordinator, context=None) -> None:
        self.coordinator = coordinator
        self.coordinator_context = context

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success


# --- homeassistant.helpers.device_registry -----------------------------------


class FakeDeviceRegistry:
    """Device registry stand-in supporting via_device_id resolution."""

    def __init__(self) -> None:
        self._devices: dict = {}

    def register(self, identifier, config_entry_id: str, device_id: str) -> None:
        self._devices[(identifier, config_entry_id)] = SimpleNamespace(id=device_id)

    def async_get_device_by_identifier(self, identifier, config_entry_id):
        return self._devices.get((identifier, config_entry_id))


fake_device_registry = FakeDeviceRegistry()


# --- homeassistant.config_entries / data_entry_flow --------------------------


class ConfigEntry(_Generic):
    """Config entry stand-in with data, options and runtime_data."""

    def __init__(self, data=None, options=None, entry_id: str = "entry-123") -> None:
        self.data = dict(data or {})
        self.options = dict(options or {})
        self.entry_id = entry_id
        self.runtime_data = None


class _FlowBase:
    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def async_abort(self, **kwargs):
        return {"type": "abort", **kwargs}


class ConfigFlow(_FlowBase):
    """Config flow stand-in; tests set hass and the entry being reauthenticated."""

    hass = None
    reauth_entry: ConfigEntry | None = None

    def __init_subclass__(cls, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.domain = domain

    async def async_set_unique_id(self, unique_id):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self) -> None:
        """Nothing is configured in tests."""

    def _get_reauth_entry(self) -> ConfigEntry:
        return self.reauth_entry

    def async_update_reload_and_abort(self, entry, *, data_updates=None, **kwargs):
        entry.data.update(data_updates or {})
        return {"type": "abort", "reason": "reauth_successful"}


class OptionsFlow(_FlowBase):
    """Options flow stand-in; tests set config_entry."""

    config_entry: ConfigEntry | None = None


class OptionsFlowWithReload(OptionsFlow):
    automatic_reload = True


class HomeAssistantError(Exception):
    """Base Home Assistant error."""


class ConfigEntryAuthFailed(HomeAssistantError):
    """Raised to start the reauth flow."""


def _install() -> None:
    if getattr(sys.modules.get("homeassistant"), "_weishaupt_stub", False):
        return

    _module(
        "aiohttp",
        ClientError=ClientError,
        ClientConnectorError=ClientConnectorError,
        BasicAuth=BasicAuth,
        ClientTimeout=ClientTimeout,
        ClientSession=ClientSession,
    )

    _module("homeassistant", package=True, _weishaupt_stub=True)
    _module("homeassistant.components", package=True)
    _module(
        "homeassistant.components.sensor",
        SensorEntity=type("SensorEntity", (), {}),
        SensorDeviceClass=_NameEnum(),
        SensorStateClass=_NameEnum(),
    )
    _module(
        "homeassistant.components.binary_sensor",
        BinarySensorEntity=type("BinarySensorEntity", (), {}),
        BinarySensorDeviceClass=_NameEnum(),
    )
    _module("homeassistant.components.switch", SwitchEntity=type("SwitchEntity", (), {}))
    _module("homeassistant.components.select", SelectEntity=type("SelectEntity", (), {}))
    _module("homeassistant.components.button", ButtonEntity=type("ButtonEntity", (), {}))

    def async_redact_data(data, to_redact):
        if isinstance(data, dict):
            return {
                key: "**REDACTED**" if key in to_redact else async_redact_data(value, to_redact)
                for key, value in data.items()
            }
        if isinstance(data, list):
            return [async_redact_data(value, to_redact) for value in data]
        return data

    _module("homeassistant.components.diagnostics", async_redact_data=async_redact_data)

    _module(
        "homeassistant.config_entries",
        ConfigEntry=ConfigEntry,
        ConfigFlow=ConfigFlow,
        OptionsFlow=OptionsFlow,
        OptionsFlowWithReload=OptionsFlowWithReload,
        ConfigFlowResult=dict,
    )
    _module("homeassistant.core", HomeAssistant=object, callback=lambda func: func)
    _module(
        "homeassistant.exceptions",
        HomeAssistantError=HomeAssistantError,
        ConfigEntryAuthFailed=ConfigEntryAuthFailed,
    )
    _module(
        "homeassistant.const",
        CONF_HOST="host",
        CONF_PASSWORD="password",
        CONF_USERNAME="username",
        PERCENTAGE="%",
        EntityCategory=SimpleNamespace(CONFIG="config", DIAGNOSTIC="diagnostic"),
        Platform=_NameEnum(),
        UnitOfEnergy=SimpleNamespace(KILO_WATT_HOUR="kWh"),
        UnitOfPower=SimpleNamespace(KILO_WATT="kW"),
        UnitOfPressure=SimpleNamespace(BAR="bar"),
        UnitOfTemperature=SimpleNamespace(CELSIUS="°C"),
        UnitOfTime=SimpleNamespace(HOURS="h"),
    )
    _module("homeassistant.helpers", package=True)
    _module(
        "homeassistant.helpers.device_registry",
        DeviceInfo=dict,
        async_get=lambda hass: fake_device_registry,
    )
    _module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    _module(
        "homeassistant.helpers.entity",
        EntityCategory=SimpleNamespace(CONFIG="config", DIAGNOSTIC="diagnostic"),
    )
    _module(
        "homeassistant.helpers.aiohttp_client",
        async_get_clientsession=lambda hass: ClientSession(),
    )
    _module(
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=CoordinatorEntity,
        DataUpdateCoordinator=DataUpdateCoordinator,
        UpdateFailed=UpdateFailed,
    )

    custom_components = _module("custom_components", package=True)
    custom_components.__path__ = [str(REPO_ROOT / "custom_components")]
    integration = _module(PACKAGE, package=True)
    integration.__path__ = [str(PACKAGE_ROOT)]


def load(name: str) -> ModuleType:
    """Return the integration module `name` (e.g. "api"), loading it from disk once."""
    full_name = f"{PACKAGE}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PACKAGE_ROOT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[full_name]
        raise
    return module


_install()
