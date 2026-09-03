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
3. During an adaptive band hop, FT8-AutoPilot waits for a WSJT-X Status packet
   confirming the requested dial frequency. It then waits for RX, sends the
   configured gain with `EX010414xxx;`, and reads `EX010414;` to confirm when
   the radio returns a value.
4. Gain writes remain RX-only. No ALC/PO sampling or automatic gain adjustment
   is performed.

Examples:

```
EX010414003;
EX010414010;
EX010414025;
```

## Manual Band Profiles

Profiles contain only `usb_mod_gain` and `tx_audio_attenuation` for each band.
Use SAVE CURRENT BAND, DELETE CURRENT BAND PROFILE, RESET ALL BAND PROFILES,
and AUTO APPLY BAND PROFILES to manage them. The operator chooses all values;
there is no automatic learning, ALC/PO protection, or TX-time gain adjustment.
