"""Config flow tests."""

from unittest.mock import patch

import pytest

from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.beurer_bf700.const import DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .common import ADDRESS, NAME, service_info

pytestmark = pytest.mark.usefixtures("mock_bluetooth")


@pytest.fixture(autouse=True)
def no_setup():
    """Do not set up the entry after the flow."""
    with patch("custom_components.beurer_bf700.async_setup_entry", return_value=True):
        yield


async def test_bluetooth_discovery(hass: HomeAssistant) -> None:
    """A discovered scale is confirmed and added."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info(0x6A855803)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == NAME
    assert result["result"].unique_id == ADDRESS


async def test_bluetooth_discovery_already_configured(hass: HomeAssistant) -> None:
    """A configured scale is not offered again."""
    MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, title=NAME).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_bluetooth_discovery_unsupported(hass: HomeAssistant) -> None:
    """Other devices are rejected."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info(name="Beurer BM54")
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"


async def test_user_flow(hass: HomeAssistant) -> None:
    """The user picks a scale Home Assistant has seen."""
    with patch(
        "custom_components.beurer_bf700.config_flow.async_discovered_service_info",
        return_value=[service_info(), service_info(name="Other", address="AA:BB:CC:DD:EE:FF")],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: ADDRESS}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == NAME
    assert result["result"].unique_id == ADDRESS


async def test_user_flow_nothing_found(hass: HomeAssistant) -> None:
    """Without a scale in range the flow aborts."""
    with patch(
        "custom_components.beurer_bf700.config_flow.async_discovered_service_info",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"
