"""Config + options flow for the Waven integration.

Config flow (spec §4): paste API key → we validate against ``GET /api/v1/user``
and surface the tier → create the entry. Options flow: the hybrid voice panel
(a voice per response category), STT model, daily cap, retention, audit toggle.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    WavenAuthError,
    WavenClient,
    WavenConnectionError,
    WavenConsentError,
    WavenError,
    WavenQuotaError,
)
from .const import (
    CONF_API_KEY,
    CONF_AUDIT_ENABLED,
    CONF_DAILY_CAP_MINUTES,
    CONF_REGION,
    CONF_RETAIN_AUDIO,
    CONF_STT_LANGUAGE,
    CONF_STT_MODE,
    CONF_TTS_FORMAT,
    CONF_VOICE_BY_CATEGORY,
    DEFAULT_CATEGORY_VOICES,
    DEFAULT_DAILY_CAP_MINUTES,
    DEFAULT_REGION,
    DEFAULT_STT_LANGUAGE,
    DEFAULT_STT_MODE,
    DEFAULT_TTS_FORMAT,
    DOMAIN,
    KOKORO_VOICES,
    MAX_DAILY_CAP_MINUTES,
    MIN_DAILY_CAP_MINUTES,
    REGION_HOSTS,
    STT_MODES,
    STT_SUPPORTED_LANGUAGES,
    TTS_FORMATS,
)
from .routing import build_voice_options

_LOGGER = logging.getLogger(__name__)


async def _validate_key(hass, api_key: str, region: str, retain_audio: bool = True):
    """Validate an API key; return the parsed account or raise.

    ``retain_audio`` is the household's stored preference when one exists
    (reauth), so even this GET carries the opt-out header — the privacy copy
    promises it on *every* request. Initial setup has no stored preference
    yet and keeps the default.
    """
    session = async_get_clientsession(hass)
    host = REGION_HOSTS.get(region, REGION_HOSTS[DEFAULT_REGION])
    client = WavenClient(session, api_key, host)
    return await client.async_validate(retain_audio=retain_audio)


def _account_unique_id(account, api_key: str) -> str:
    """The entry's unique id for an account. Kept in one place so setup and
    reauth can't drift into disagreeing about what "the same account" means."""
    return account.email or api_key[:12]


class WavenConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup and reauth."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            region = user_input.get(CONF_REGION, DEFAULT_REGION)
            try:
                account = await _validate_key(self.hass, api_key, region)
            except WavenAuthError:
                errors["base"] = "invalid_auth"
            except WavenConsentError:
                # HTTP 428 — a real, fixable account state, not a bug. Telling
                # the user "unexpected error" sent them to support instead of
                # to the one dashboard checkbox that fixes it.
                errors["base"] = "consent_required"
            except WavenQuotaError:
                errors["base"] = "quota_exceeded"
            except WavenConnectionError:
                errors["base"] = "cannot_connect"
            except WavenError:
                errors["base"] = "unknown"
            else:
                unique = _account_unique_id(account, api_key)
                await self.async_set_unique_id(unique)
                self._abort_if_unique_id_configured()
                title = f"Waven ({account.email})" if account.email else "Waven"
                return self.async_create_entry(
                    title=title,
                    data={CONF_API_KEY: api_key, CONF_REGION: region},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self._user_schema(),
            errors=errors,
        )

    @callback
    def _user_schema(self) -> vol.Schema:
        schema: dict[Any, Any] = {
            vol.Required(CONF_API_KEY): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }
        # Only prompt for a region once more than one edge exists.
        if len(REGION_HOSTS) > 1:
            schema[vol.Optional(CONF_REGION, default=DEFAULT_REGION)] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=r, label=r.upper())
                        for r in REGION_HOSTS
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        return vol.Schema(schema)

    # --- reauth --------------------------------------------------------------
    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            region = entry.data.get(CONF_REGION, DEFAULT_REGION)
            try:
                account = await _validate_key(
                    self.hass, api_key, region,
                    retain_audio=bool(entry.options.get(CONF_RETAIN_AUDIO, True)),
                )
            except WavenAuthError:
                errors["base"] = "invalid_auth"
            except WavenConsentError:
                errors["base"] = "consent_required"
            except WavenQuotaError:
                errors["base"] = "quota_exceeded"
            except WavenConnectionError:
                errors["base"] = "cannot_connect"
            except WavenError:
                errors["base"] = "unknown"
            else:
                # Reauth must re-authenticate the SAME account, not just some
                # valid account. Without this check, pasting a second
                # household's (or a colleague's) key silently rebound this
                # entry — with all its options, its device, its entity ids and
                # its usage history — to a different Waven account, and the
                # only visible change was the minute pool moving.
                #
                # Only the EMAIL identifies an account. `_account_unique_id`
                # falls back to the key prefix when the backend returns no
                # email, and that prefix changes when the key is rotated — the
                # exact thing reauth exists to do — so comparing it would abort
                # every legitimate rotation on such an entry. When the identity
                # isn't account-derived we have nothing to check against, so we
                # accept the key (the pre-existing behaviour for those entries).
                if entry.unique_id and account.email and account.email != entry.unique_id:
                    return self.async_abort(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, CONF_API_KEY: api_key}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return WavenOptionsFlow()


class WavenOptionsFlow(OptionsFlow):
    """The voice panel + STT/cap/privacy settings."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Fetch the user's cloned voices so the picker lists stock + clones.
        session = async_get_clientsession(self.hass)
        region = self.config_entry.data.get(CONF_REGION, DEFAULT_REGION)
        host = REGION_HOSTS.get(region, REGION_HOSTS[DEFAULT_REGION])
        api_key = self.config_entry.data.get(CONF_API_KEY, "")
        # The options form is being *built*, so the value the user is about to
        # submit isn't known yet — send the currently stored preference, the
        # same one the coordinator's gallery refresh uses. (This GET carries no
        # audio for the flag to act on; it is sent so "every request" is true.)
        gallery = await WavenClient(session, api_key, host).async_list_voices(
            retain_audio=bool(self.config_entry.options.get(CONF_RETAIN_AUDIO, True)),
        )

        def current(key: str, default):
            return self.config_entry.options.get(key, default)

        voice_choices = [
            SelectOptionDict(value=o.value, label=o.label)
            for o in build_voice_options(KOKORO_VOICES, gallery)
        ]
        # `async_list_voices` returns [] on ANY failure (that is deliberate —
        # a flaky gallery call must not block the options panel). But a cloned
        # voice that is already configured then vanishes from its own dropdown,
        # and vol rejects a default that isn't in the option list: the form
        # became un-submittable, and a user who worked around it by picking
        # another voice silently lost the clone assignment. Re-add any
        # configured value the gallery didn't return, labelled so it's clear
        # the list is degraded rather than the voice deleted.
        known = {c["value"] for c in voice_choices}
        for conf_key in CONF_VOICE_BY_CATEGORY.values():
            configured = current(conf_key, None)
            if configured and configured not in known:
                known.add(configured)
                voice_choices.append(
                    SelectOptionDict(
                        value=configured,
                        label=f"{configured} (currently unavailable)",
                    )
                )

        voice_selector = SelectSelector(
            SelectSelectorConfig(options=voice_choices, mode=SelectSelectorMode.DROPDOWN)
        )

        schema: dict[Any, Any] = {}
        for category, conf_key in CONF_VOICE_BY_CATEGORY.items():
            schema[
                vol.Required(
                    conf_key,
                    default=current(conf_key, DEFAULT_CATEGORY_VOICES[category]),
                )
            ] = voice_selector

        schema[
            vol.Required(CONF_STT_MODE, default=current(CONF_STT_MODE, DEFAULT_STT_MODE))
        ] = SelectSelector(
            SelectSelectorConfig(
                options=[SelectOptionDict(value=m, label=m) for m in STT_MODES],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="stt_mode",
            )
        )
        schema[
            vol.Required(
                CONF_STT_LANGUAGE, default=current(CONF_STT_LANGUAGE, DEFAULT_STT_LANGUAGE)
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=lang, label=lang.upper())
                    for lang in STT_SUPPORTED_LANGUAGES
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
        schema[
            vol.Required(
                CONF_TTS_FORMAT, default=current(CONF_TTS_FORMAT, DEFAULT_TTS_FORMAT)
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=[SelectOptionDict(value=f, label=f.upper()) for f in TTS_FORMATS],
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
        schema[
            vol.Required(
                CONF_DAILY_CAP_MINUTES,
                default=current(CONF_DAILY_CAP_MINUTES, DEFAULT_DAILY_CAP_MINUTES),
            )
        ] = NumberSelector(
            NumberSelectorConfig(
                min=MIN_DAILY_CAP_MINUTES,
                max=MAX_DAILY_CAP_MINUTES,
                step=5,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="min",
            )
        )
        schema[
            vol.Required(CONF_RETAIN_AUDIO, default=current(CONF_RETAIN_AUDIO, True))
        ] = BooleanSelector()
        schema[
            vol.Required(CONF_AUDIT_ENABLED, default=current(CONF_AUDIT_ENABLED, True))
        ] = BooleanSelector()

        if len(REGION_HOSTS) > 1:
            schema[
                vol.Required(CONF_REGION, default=current(CONF_REGION, region))
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=r, label=r.upper()) for r in REGION_HOSTS
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )

        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
