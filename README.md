# FT8-AutoPilot

FT8-AutoPilot is an experimental Windows companion for WSJT-X. It selects
eligible FT8/FT4 CQ or direct callers, sends the standard WSJT-X UDP Reply,
and tracks the QSO lifecycle. The station operator remains responsible for all
transmissions and regulatory compliance.

## Current behavior

- CQ and addressed direct callers are selected using freshness, confidence,
  duplicate, blacklist, DXCC, continent, activity, and signal rules.
- A selected station receives a standard UDP Reply. WSJT-X alone manages TX
  audio DF and Hold Tx Freq behavior.
- Autopilot does not scan for a free TX slot, set TX DF, lock TX DF, restore TX
  DF, or disarm because TX DF changes.
- QSO lifecycle, terminal-message retries, pending direct callers, and safety
  checks remain active.
- Band hopping can request a new dial frequency after a safe RX transition.

## AP1 controls

The companion WSJT-X AP1 fork reserves type 18 and keeps these wire IDs:

| Type | AP1 message |
| ---: | --- |
| 18 | retired/reserved |
| 19 | SetTxPeriod |
| 20 | SetDialFrequency |
| 21 | SetTxAudioAttenuation |
| 22 | QueryTxAudioAttenuation |
| 23 | TxAudioAttenuationState |

SetTxPeriod supports adaptive TX period selection. SetDialFrequency supports
band hopping. Types 21 through 23 maintain the WSJT-X TX attenuation state.
Direct-call Reply support requires the AP1 WSJT-X fork; ordinary CQ Reply uses
the standard WSJT-X UDP message.

## FTX-1 CAT-2

FTX-1 CAT-2 is opt-in and uses only the FTX-1 Standard COM/CAT-2 port. It
never uses the WSJT-X CAT port or sends PTT. Per-band manual profiles retain:

- `usb_mod_gain`
- `tx_audio_attenuation`

Use SAVE CURRENT BAND, DELETE CURRENT BAND PROFILE, RESET ALL BAND PROFILES,
and AUTO APPLY BAND PROFILES to manage profiles. There is no automatic drive
learning, ALC/PO measurement, plateau search, backoff, or TX-time drive change.
See [docs/FTX1_CAT2.md](docs/FTX1_CAT2.md).

## Build and test

```powershell
python -m compileall -q src tests
python -m pytest -q
pyinstaller -y packaging/wsjtx-autopilot.spec
```

The packaged application is written to:

```text
dist\WSJTX-AutoPilot\WSJTX-AutoPilot.exe
```

## User data

Application settings, the QSO database, and logs live below the Windows AppData
locations selected by `AppPaths`. They are not stored in this repository and
are not included in builds or commits.

## License

GPL-3.0-or-later
