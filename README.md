# FT8-AutoPilot

FT8-AutoPilot is an experimental Windows companion for WSJT-X. It selects
eligible FT8/FT4 CQ or direct callers, sends the standard WSJT-X UDP Reply,
and tracks the QSO lifecycle. The station operator remains responsible for all
transmissions and regulatory compliance.

## Setup

Install the GUI and test dependencies, start WSJT-X with its UDP server
enabled, then start the AutoPilot GUI:

```powershell
python -m pip install -e ".[gui,test,build]"
wsjtx-autopilot-gui
```

Configure the local callsign and WSJT-X UDP port in Preferences. Direct-call
Reply, AP1 controls, and automatic band hopping require the compatible custom
WSJT-X/AP1 integration described below. Ordinary CQ Reply uses the standard
WSJT-X UDP message.

## Generic AutoPilot features

- CQ and addressed direct callers are selected using freshness, confidence,
  duplicate, blacklist, DXCC, continent, activity, and signal rules.
- A fresh direct caller has priority over an unanswered queued CQ when it is
  safe to switch. It never preempts a progressed QSO, an active transmission,
  or terminal-message finalization.
- A selected station receives a standard UDP Reply. WSJT-X alone manages TX
  audio DF and Hold Tx Freq behavior.
- Autopilot does not scan for a free TX slot, set TX DF, lock TX DF, restore TX
  DF, or disarm because TX DF changes.
- QSO lifecycle, terminal-message retries, pending direct callers, and safety
  checks remain active.
- Adaptive hopping tracks the useful station pool on the current band instead
  of relying on a fixed failure counter. It can try the other TX cycle first,
  then request a new dial frequency after a safe RX transition when the useful
  pool is exhausted.
- Optional forced time-based hopping cycles the configured ordered bands only
  while idle, confirms each AP1 frequency change from WSJT-X Status, and
  pauses new calls while the new band settles.
- The compact GUI shows decode activity, candidate ranking, adaptive state,
  and the current `Working <CALL> <COUNTRY>` status.

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

## Forced band hopping

The GUI stores settings as JSON using the existing naming convention:

```json
{
  "forced_band_hopping_enabled": true,
  "forced_band_hop_bands": ["20m", "30m", "40m"],
  "forced_band_hop_interval_minutes": 10,
  "forced_band_hop_settle_seconds": 30
}
```

`forced_band_hop_bands` controls the sequence. It shares
`auto_hop_band_frequencies` and the standard FT8 frequency map with adaptive
hopping, but does not inherit adaptive hopping's band list. Forced hopping
defaults to disabled, a 10-minute dwell, and a 30-second post-confirmation
settle period. Frequency changes are sent through Hamlib. When automatic drive
profiles are enabled, the destination's WSJT-X attenuation is explicitly confirmed by an
AP1 type-23 state packet before settling begins. A missing or unconfirmed
destination profile leaves automatic TX blocked while RX continues.

## Radio control through Hamlib

Radio control is opt-in. Select the Hamlib model number, CAT port and baud rate.
Frequency, PTT safety, ALC, SWR, RF power and tuner commands use Hamlib whenever
the selected backend exposes them. The radio is identified at startup when its
CAT protocol supports identification; otherwise the selected Hamlib model is
used as the compatibility boundary. Per-band profiles retain only the effective
WSJT-X audio attenuation; the legacy `usb_mod_gain` JSON field is ignored and
kept only for settings compatibility.

- `tx_audio_attenuation`

Use SAVE CURRENT BAND, DELETE CURRENT BAND PROFILE, RESET ALL BAND PROFILES,
and AUTO APPLY BAND PROFILES to manage profiles. AutoPilot never reads or writes
USB MOD GAIN. Hamlib reads ALC, SWR and RF power when supported. AutoPilot corrects audio
attenuation in both directions after each measured TX period. Consecutive SWR
readings over the configured limit immediately halt TX, disarm AutoPilot, and
raise the red antenna-safety alert. Automatic tuner recovery and automatic band
exclusion are currently disabled; a safety trip never starts a TUNE command or
changes band.
See [docs/FTX1_CAT2.md](docs/FTX1_CAT2.md).

## Known limitations

- The bundled offline DXCC resolver covers common prefixes and special entities
  needed by the current UI, but it is not a complete CTY database. An optional
  configured CTY.DAT file takes precedence when available.
- Country reference distance and bearing are approximate. Exact values require
  a valid Maidenhead locator decoded from FT8 traffic and the local station
  locator received in WSJT-X Status.
- AutoPilot does not select a free TX slot or set, lock, or restore TX DF;
  WSJT-X remains responsible for those controls.
- Meter availability and units depend on the selected radio and Hamlib backend.
  ALC regulation and SWR protection should only be enabled after validating the
  reported values on the connected station.

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
