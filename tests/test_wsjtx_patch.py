from pathlib import Path


PATCH = Path(__file__).parents[1] / "patches" / "wsjtx-3.2-direct-reply.patch"


def test_direct_reply_patch_contains_no_tx_df_automation() -> None:
    text = PATCH.read_text(encoding="utf-8")
    assert "Set" + "TxDF" not in text
    assert "set_tx_df" not in text
    assert "m_networkTxDfPending" not in text


def test_patch_contains_no_gui_automation() -> None:
    text = PATCH.read_text(encoding="utf-8").lower()
    for forbidden in ("pyautogui", "mouse coordinates", "sendkeys", "ocr"):
        assert forbidden not in text
