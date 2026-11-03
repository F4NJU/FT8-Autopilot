from dataclasses import replace

from wsjtx_autopilot.config import AppConfig
from wsjtx_autopilot.runtime import AutopilotRuntime, HuntRemoteSpot
from wsjtx_autopilot.special_calls.models import SpecialCallActivity

from test_runtime import NOW, CollectingControl, decode, status


def target(callsign: str = "TM85VGC", band: str = "20m") -> SpecialCallActivity:
    return SpecialCallActivity(
        callsign, "France", "F", "EU", NOW, NOW, (band,), (12,), NOW, ("cluster",), ("FT8",),
        path_score=95, confidence="forte", best_band=band,
    )


def hunt_runtime() -> tuple[AutopilotRuntime, CollectingControl]:
    control = CollectingControl()
    config = replace(
        AppConfig(),
        candidate_collection_seconds=0,
        allowed_auto_hop_bands=("20m", "40m"),
    )
    app = AutopilotRuntime(config, control=control)
    app.handle(status(), NOW)
    assert app.configure_hunt((target(),), "TM85VGC")
    return app, control


def test_hunt_never_calls_an_unselected_cq() -> None:
    app, control = hunt_runtime()

    app.handle(decode("CQ EA7CTA IM66", -4, 1000), NOW)

    assert control.actions == []


def test_hunt_calls_target_only_after_local_cq_decode() -> None:
    app, control = hunt_runtime()

    action = app.handle(decode("CQ TM85VGC JN18", -8, 1100), NOW)

    assert action is not None
    assert action.station == "TM85VGC"
    assert [item.station for item in control.actions] == ["TM85VGC"]


def test_direct_call_keeps_priority_during_hunt() -> None:
    app, control = hunt_runtime()

    action = app.handle(decode("F4NJU EA7CTA -12", -12, 1200), NOW)

    assert action is not None
    assert action.station == "EA7CTA"
    assert [item.station for item in control.actions] == ["EA7CTA"]


def test_hunt_refuses_targets_outside_allowed_bands() -> None:
    app, _control = hunt_runtime()

    assert not app.configure_hunt((target("5W0ZP", "10m"),), "5W0ZP")
    assert not app.hunt_snapshot()["active"]


def test_hunt_uses_another_target_band_after_swr_exclusion() -> None:
    app, control = hunt_runtime()
    control.armed = True
    control.set_dial_frequency = lambda _instance_id, _frequency: True
    multi_band = replace(target(), bands=("20m", "40m"), best_band="20m")
    assert app.configure_hunt((multi_band,), "TM85VGC")
    app._swr_excluded_bands.add("20m")

    app._run_hunt(NOW)

    assert app._pending_dial_frequency is not None
    assert app._pending_dial_frequency.frequency_hz == app._band_frequency("40m", "FT8")


def test_reply_rare_spot_changes_to_allowed_remote_band_only() -> None:
    app, control = hunt_runtime()
    app.stop_hunt()
    control.armed = True
    control.set_dial_frequency = lambda _instance_id, _frequency: True
    app.set_reply_rare_targets((replace(target(), bands=("20m", "40m"), best_band="20m"),))
    app.set_remote_hunt_enabled(True)
    assert app.note_hunt_remote_spot(HuntRemoteSpot("TM85VGC", "40m", NOW))

    app._run_reply_rare_watch(NOW)

    assert app._pending_dial_frequency is not None
    assert app._pending_dial_frequency.frequency_hz == app._band_frequency("40m", "FT8")


def test_reply_rare_spot_uses_the_nearest_configured_ft8_scan_channel() -> None:
    app, control = hunt_runtime()
    app.stop_hunt()
    control.armed = True
    control.set_dial_frequency = lambda _instance_id, _frequency: True
    app.status.dial_frequency = 14_074_000
    app.set_reply_rare_targets((target(),))
    app.set_remote_hunt_enabled(True)
    assert app.note_hunt_remote_spot(HuntRemoteSpot("TM85VGC", "20m", NOW, frequency_hz=14_105_000))

    app._run_reply_rare_watch(NOW)

    assert app._pending_dial_frequency is not None
    assert app._pending_dial_frequency.frequency_hz == 14_090_000


def test_reply_rare_spot_near_the_primary_channel_does_not_use_its_exact_offset() -> None:
    app, control = hunt_runtime()
    app.stop_hunt()
    control.armed = True
    control.set_dial_frequency = lambda _instance_id, _frequency: True
    app.status.dial_frequency = 14_090_000
    app.set_reply_rare_targets((target(),))
    app.set_remote_hunt_enabled(True)
    assert app.note_hunt_remote_spot(HuntRemoteSpot("TM85VGC", "20m", NOW, frequency_hz=14_075_553))

    app._run_reply_rare_watch(NOW)

    assert app._pending_dial_frequency is not None
    assert app._pending_dial_frequency.frequency_hz == 14_074_000


def test_reply_unconfirmed_dxcc_spot_changes_to_its_remote_band_only() -> None:
    app, control = hunt_runtime()
    app.stop_hunt()
    control.armed = True
    control.set_dial_frequency = lambda _instance_id, _frequency: True
    app.set_unconfirmed_dxcc_follow_enabled(True)
    assert app.note_unconfirmed_dxcc_remote_spot(
        HuntRemoteSpot("XX9TXN", "40m", NOW, transmitter_grid="IO91AA", receiver_grid="JN28AA")
    )

    app._run_unconfirmed_dxcc_watch(NOW)

    assert app._pending_dial_frequency is not None
    assert app._pending_dial_frequency.frequency_hz == app._band_frequency("40m", "FT8")


def test_adaptive_scan_visits_the_secondary_ft8_channel_before_the_next_band() -> None:
    app, _control = hunt_runtime()

    assert app._next_auto_hop_destination() == ("20m", 14_090_000)

    app.status.dial_frequency = 14_090_000

    assert app._next_auto_hop_destination() == ("40m", 7_074_000)
