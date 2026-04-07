"""Config flow for BJC Newsletter integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_GEMINI_API_KEY,
    CONF_GEMINI_MODEL,
    DEFAULT_GEMINI_MODEL,
    DOMAIN,
    NAME,
    OPT_BROWSERBASE_API_KEY,
    OPT_BROWSERBASE_PROJECT_ID,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_GEMINI_API_KEY): str,
        vol.Optional(CONF_GEMINI_MODEL, default=DEFAULT_GEMINI_MODEL): str,
    }
)


async def _validate_gemini_key(hass, api_key: str, model: str) -> None:
    """Validate the Gemini API key with a minimal test call.

    Raises ValueError on auth failure, ConnectionError on network issues.
    Runs in executor since google-generativeai is synchronous.
    """

    def _check() -> None:
        from google import genai

        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=model,
            contents="Reply with one word: OK",
        )
        if not resp.text:
            raise ValueError("Empty response from Gemini — key may be invalid")

    try:
        await hass.async_add_executor_job(_check)
    except Exception as err:
        err_lower = str(err).lower()
        if any(kw in err_lower for kw in ("api_key", "unauthorized", "invalid", "permission", "403")):
            raise ValueError(f"Invalid Gemini API key: {err}") from err
        raise ConnectionError(f"Cannot reach Gemini API: {err}") from err


class BJCNewsletterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for BJC Newsletter."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Collect and validate the Gemini API key."""
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key = user_input[CONF_GEMINI_API_KEY].strip()
            model = user_input.get(CONF_GEMINI_MODEL, DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL

            try:
                await _validate_gemini_key(self.hass, api_key, model)
            except ValueError:
                errors["base"] = "invalid_auth"
            except ConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating Gemini API key")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=NAME,
                    data={
                        CONF_GEMINI_API_KEY: api_key,
                        CONF_GEMINI_MODEL: model,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
            description_placeholders={"name": NAME},
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> BJCNewsletterOptionsFlow:
        return BJCNewsletterOptionsFlow(config_entry)


class BJCNewsletterOptionsFlow(config_entries.OptionsFlow):
    """Allow updating the Gemini API key or model after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            api_key = user_input[CONF_GEMINI_API_KEY].strip()
            model = (user_input.get(CONF_GEMINI_MODEL) or DEFAULT_GEMINI_MODEL).strip()
            bb_key = user_input.get(OPT_BROWSERBASE_API_KEY, "").strip()
            bb_project = user_input.get(OPT_BROWSERBASE_PROJECT_ID, "").strip()
            try:
                await _validate_gemini_key(self.hass, api_key, model)
            except ValueError:
                errors["base"] = "invalid_auth"
            except ConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating Gemini API key")
                errors["base"] = "unknown"
            else:
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={
                        **self.config_entry.data,
                        CONF_GEMINI_API_KEY: api_key,
                        CONF_GEMINI_MODEL: model,
                    },
                    options={
                        **self.config_entry.options,
                        OPT_BROWSERBASE_API_KEY: bb_key,
                        OPT_BROWSERBASE_PROJECT_ID: bb_project,
                    },
                )
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GEMINI_API_KEY,
                        default=self.config_entry.data.get(CONF_GEMINI_API_KEY, ""),
                    ): str,
                    vol.Optional(
                        CONF_GEMINI_MODEL,
                        default=self.config_entry.data.get(
                            CONF_GEMINI_MODEL, DEFAULT_GEMINI_MODEL
                        ),
                    ): str,
                    vol.Optional(
                        OPT_BROWSERBASE_API_KEY,
                        default=self.config_entry.options.get(OPT_BROWSERBASE_API_KEY, ""),
                    ): str,
                    vol.Optional(
                        OPT_BROWSERBASE_PROJECT_ID,
                        default=self.config_entry.options.get(OPT_BROWSERBASE_PROJECT_ID, ""),
                    ): str,
                }
            ),
            errors=errors,
        )
