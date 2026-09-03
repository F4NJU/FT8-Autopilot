import json

from wsjtx_autopilot.config import ActivityPolicy, DirectCallPolicy, SettingsStore, UserSettings


def test_settings_round_trip_without_runtime_arming(tmp_path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    settings = UserSettings(
        local_callsign=" f4nju ",
        preferred_continents={"eu"},
        preferred_dxcc={"on"},
        direct_call_policy=DirectCallPolicy.NORMAL,
        blacklist={" dl1bad "},
        pota_policy=ActivityPolicy.PRIORITY,
        respond_to_cq_dx=True,
        max_no_progress_periods=12,
        stalled_qso_cooldown_seconds=420,
        remote_busy_cooldown_seconds=240,
        max_remote_cq_during_attempt=3,
        remote_returned_to_cq_cooldown_seconds=75,
        finalization_hold_periods=2,
        final_tx_timeout_periods=3,
        max_final_retries=4,
        pending_direct_ttl_seconds=90,
        ftx1_cat2_enabled=True,
        ftx1_cat2_confirmed_ftx1=True,
        ftx1_cat2_port="COM5",
        ftx1_cat2_baudrate=38_400,
        ftx1_cat2_timeout_seconds=0.4,
        ftx1_auto_apply_band_profiles=True,
        ftx1_band_profiles={"20m": {"usb_mod_gain": 35, "tx_audio_attenuation": 118}, "40m": {"usb_mod_gain": 32, "tx_audio_attenuation": 145}},
    )

    store.save(settings)
    loaded = store.load()
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert loaded.local_callsign == "F4NJU"
    assert loaded.preferred_continents == {"EU"}
    assert loaded.preferred_dxcc == {"ON"}
    assert loaded.direct_call_policy is DirectCallPolicy.NORMAL
    assert loaded.blacklist == {"DL1BAD"}
    assert loaded.pota_policy is ActivityPolicy.PRIORITY
    assert loaded.respond_to_cq_dx
    assert loaded.max_no_progress_periods == 12
    assert loaded.stalled_qso_cooldown_seconds == 420
    assert loaded.remote_busy_cooldown_seconds == 240
    assert loaded.max_remote_cq_during_attempt == 3
    assert loaded.remote_returned_to_cq_cooldown_seconds == 75
    assert loaded.finalization_hold_periods == 2
    assert loaded.final_tx_timeout_periods == 3
    assert loaded.max_final_retries == 4
    assert loaded.pending_direct_ttl_seconds == 90
    assert loaded.ftx1_cat2_enabled
    assert loaded.ftx1_cat2_confirmed_ftx1
    assert loaded.ftx1_cat2_port == "COM5"
    assert loaded.ftx1_cat2_baudrate == 38_400
    assert loaded.ftx1_cat2_timeout_seconds == 0.4
    assert loaded.ftx1_auto_apply_band_profiles
    assert loaded.ftx1_band_profiles == {"20m": {"usb_mod_gain": 35, "tx_audio_attenuation": 118}, "40m": {"usb_mod_gain": 32, "tx_audio_attenuation": 145}}
    assert "armed" not in payload


def test_invalid_settings_fall_back_safely(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"direct_call_policy": "bad", "blacklist": null}', encoding="utf-8")

    loaded = SettingsStore(path).load()

    assert loaded.direct_call_policy is DirectCallPolicy.ALWAYS_PRIORITY
    assert loaded.blacklist == set()


def test_non_object_settings_fall_back_safely(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("[]", encoding="utf-8")

    assert SettingsStore(path).load() == UserSettings()


def test_legacy_retired_options_are_ignored_and_removed_on_save(tmp_path) -> None:
    path = tmp_path / "settings.json"
    retired_frequency = "_".join(("smart", "tx", "frequency"))
    retired_gap = "_".join(("smart", "tx", "find", "free"))
    path.write_text(
        json.dumps({retired_frequency: True, retired_gap: False}),
        encoding="utf-8",
    )

    loaded = SettingsStore(path).load()

    SettingsStore(path).save(loaded)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert retired_frequency not in payload
    assert retired_gap not in payload


def test_settings_store_accepts_qt_string_enum_values(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    settings = UserSettings(
        direct_call_policy="normal",  # type: ignore[arg-type]
        pota_policy="priority",  # type: ignore[arg-type]
    )

    store.save(settings)

    loaded = store.load()
    assert loaded.direct_call_policy is DirectCallPolicy.NORMAL
    assert loaded.pota_policy is ActivityPolicy.PRIORITY


def test_ftx1_profiles_ignore_legacy_learning_data(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.path.write_text('{"ftx1_auto_drive_enabled": true, "ftx1_drive_calibrations": {"20m/50": {"usb_mod_gain": 36}}, "ftx1_band_profiles": {"20m": {"usb_mod_gain": 35, "tx_audio_attenuation": 118}}}', encoding="utf-8")
    loaded = store.load()

    assert loaded.ftx1_band_profiles == {"20m": {"usb_mod_gain": 35, "tx_audio_attenuation": 118}}
