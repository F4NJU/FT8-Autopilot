from pathlib import Path


PATCH = Path(__file__).parents[1] / "patches" / "wsjtx-3.2-direct-reply.patch"


def test_set_tx_df_patch_uses_verified_type_and_native_change_path() -> None:
    text = PATCH.read_text(encoding="utf-8")
    assert "SetTxDF,                  // In 18" in text
    assert "Q_SIGNAL void set_tx_df (quint32 tx_df);" in text
    assert "tx_df < quint32 (ui->TxFreqSpinBox->minimum ())" in text
    assert "tx_df > quint32 (ui->TxFreqSpinBox->maximum ())" in text
    assert "ui->TxFreqSpinBox->setValue (int (tx_df));" in text
    assert "statusUpdate ();" in text


def test_reply_preserves_explicit_tx_df_independently_of_hold_tx_freq() -> None:
    text = PATCH.read_text(encoding="utf-8")
    assert "&& !m_networkTxDfPending" in text
    assert "&& (!ui->cbHoldTxFreq->isChecked () || shift || ctrl)" in text
    assert "m_networkTxDfPending = false;" in text


def test_patch_contains_no_gui_automation() -> None:
    text = PATCH.read_text(encoding="utf-8").lower()
    for forbidden in ("pyautogui", "mouse coordinates", "sendkeys", "ocr"):
        assert forbidden not in text
