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
        max_final_retries=4,
        smart_tx_frequency=False,
        smart_tx_find_free=False,
        occupied_guard_hz=80,
        tx_df_min=350,
        tx_df_max=2700,
        minimum_free_gap_hz=140,
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
    assert loaded.max_final_retries == 4
    assert not loaded.smart_tx_frequency
    assert not loaded.smart_tx_find_free
    assert loaded.occupied_guard_hz == 80
    assert loaded.tx_df_min == 350
    assert loaded.tx_df_max == 2700
    assert loaded.minimum_free_gap_hz == 140
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
