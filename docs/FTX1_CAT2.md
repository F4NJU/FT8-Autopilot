# FTX-1 CAT-2 USB MOD GAIN

## Scope

This experimental feature is implemented only for the Yaesu FTX-1. It never
shares or opens the CAT port used by WSJT-X. The operator must select the
FTX-1 Standard COM/CAT-2 port and explicitly confirm that the connected radio
is an FTX-1 before any CAT-2 connection is attempted.

CAT-2 is configured with RTS and DTR disabled. FT8-AutoPilot never sends PTT
commands through this connection. A serial error is logged, closes the CAT-2
connection, and does not stop WSJT-X automation. A later band hop retries the
connection.

## Phase 1

The feature verifies `ID0840;` before it permits FTX-1 commands. Any other
reply disables only FTX-1 CAT-2 functions.

1. Enable `FTX-1 CAT-2` in Preferences and choose the FTX-1 Standard COM port.
2. Set a value from 0 through 100 for any desired band. An empty field leaves
   USB MOD GAIN unchanged on that band.
3. During an adaptive or forced band hop, FT8-AutoPilot waits for a WSJT-X Status packet
   confirming the requested dial frequency. It then waits for RX, sends the
   configured gain with `EX010414xxx;`, restores the saved WSJT-X attenuation,
   and waits for the matching AP1 attenuation state packet before resuming.
   Forced hopping leaves automatic TX blocked if its destination profile is
   missing or the attenuation confirmation times out.
4. USB MOD GAIN writes remain RX-only. Hamlib meter sampling occurs only while
   WSJT-X reports TX; ALC corrections are applied after return to RX.

Examples:

```
EX010414003;
EX010414010;
EX010414025;
```

## Manual Band Profiles

Profiles contain only `usb_mod_gain` and `tx_audio_attenuation` for each band.
Use SAVE CURRENT BAND, DELETE CURRENT BAND PROFILE, RESET ALL BAND PROFILES,
and AUTO APPLY BAND PROFILES to manage them. USB MOD GAIN remains operator-set;
ALC and SWR are now read directly from Hamlib's FTX-1 backend (model 1051),
through a local `rigctld` process owned by AutoPilot. WSJT-X status data is not
used as the meter source.

During a transmission, AutoPilot ignores the first 1.5 seconds, then samples
Hamlib ALC, SWR, and RF power. At the end of the transmit period it adjusts the
WSJT-X audio attenuation one small step in either direction to keep ALC inside
the configured window. Samples without RF power are excluded from ALC learning.

SWR protection remains active during the transmit period. Consecutive readings
at or above the configured threshold send Halt Tx immediately and wait for a
confirmed return to RX. AutoPilot then starts one Hamlib tuner cycle. On the
FTX-1, CAT state `AC..3` confirms that tuning really started and the transition
back to `AC..1` confirms that the tuner finished. The radio temporarily selects
5 W and restores its configured power itself; AutoPilot does not write a power
value during this sequence.

The last two RF-valid SWR samples must both be below the configured limit. A
confirmed failed match excludes only the current band for the session and tries
the next allowed band. Missing tuner confirmation or insufficient meter data
stops and disarms AutoPilot without excluding the band.
