from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bale_campaign_bot.config.business import (
    BusinessConfig,
    BusinessConfigError,
    load_business_config,
    validate_business_config,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "business.example.yaml"


def valid_data() -> dict[str, Any]:
    return {
        "business": {
            "name": "نمونه",
            "short_description": "توضیح کوتاه",
            "welcome_message": "خوش آمدید",
            "about_text": "درباره کسب‌وکار",
            "contact": {"phone": "", "website": "", "social_links": []},
            "consent_text": "با عضویت دریافت پیام را می‌پذیرم",
            "consent_version": "1",
            "opt_out_confirmation_text": "لغو عضویت شد",
            "timezone": "Asia/Tehran",
            "locale": "fa",
        },
        "campaign_defaults": {
            "send_rate_per_second": 10,
            "concurrency": 5,
            "quiet_hours": {"enabled": False, "start": "22:00", "end": "08:00"},
        },
    }


def test_example_business_config_is_valid_and_rtl() -> None:
    config = load_business_config(EXAMPLE)
    assert isinstance(config, BusinessConfig)
    assert config.business.name == "کسب‌وکار نمونه"
    assert config.business.is_rtl
    assert config.business.contact.website is not None


def test_optional_empty_strings_are_normalized() -> None:
    config = validate_business_config(valid_data())
    assert config.business.contact.phone is None
    assert config.business.contact.website is None


def test_menu_requires_exactly_one_action() -> None:
    data = valid_data()
    data["business"]["default_menu"] = [{"label": "Help"}]
    with pytest.raises(BusinessConfigError, match="Business configuration is invalid") as error:
        validate_business_config(data)
    assert "exactly one of url or callback" in str(error.value)
    assert "پیکربندی کسب‌وکار معتبر نیست" in str(error.value)


@pytest.mark.parametrize(
    "button",
    [
        {"label": "both", "url": "https://example.com", "callback": "x"},
        {"label": "none", "url": "", "callback": ""},
    ],
)
def test_menu_rejects_missing_or_conflicting_action(button: dict[str, str]) -> None:
    data = valid_data()
    data["business"]["default_menu"] = [button]
    with pytest.raises(BusinessConfigError):
        validate_business_config(data)


def test_callback_obeys_bale_byte_limit() -> None:
    data = valid_data()
    data["business"]["default_menu"] = [{"label": "ok", "callback": "é" * 32}]
    validate_business_config(data)
    data["business"]["default_menu"] = [{"label": "too long", "callback": "é" * 33}]
    with pytest.raises(BusinessConfigError, match="64-byte"):
        validate_business_config(data)


def test_timezone_footer_and_quiet_hours_are_validated() -> None:
    data = valid_data()
    data["business"]["timezone"] = "Mars/Olympus"
    with pytest.raises(BusinessConfigError, match="Unknown IANA timezone"):
        validate_business_config(data)

    data = valid_data()
    data["business"]["footer_enabled"] = True
    with pytest.raises(BusinessConfigError, match="footer_text is required"):
        validate_business_config(data)

    data = valid_data()
    data["campaign_defaults"]["quiet_hours"]["start"] = "25:90"
    with pytest.raises(BusinessConfigError, match="HH:MM"):
        validate_business_config(data)


def test_unknown_fields_and_campaign_limits_are_rejected() -> None:
    data = valid_data()
    data["business"]["unexpected"] = True
    with pytest.raises(BusinessConfigError, match="Extra inputs are not permitted"):
        validate_business_config(data)

    data = valid_data()
    data["campaign_defaults"]["concurrency"] = 0
    with pytest.raises(BusinessConfigError, match="greater than or equal to 1"):
        validate_business_config(data)


def test_loader_reports_missing_malformed_and_non_mapping_yaml(tmp_path: Path) -> None:
    with pytest.raises(BusinessConfigError, match="not found"):
        load_business_config(tmp_path / "missing.yaml")
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("business: [", encoding="utf-8")
    with pytest.raises(BusinessConfigError, match="Cannot read valid YAML"):
        load_business_config(malformed)
    non_mapping = tmp_path / "root.yaml"
    non_mapping.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(BusinessConfigError, match="YAML root must be a mapping"):
        load_business_config(non_mapping)
