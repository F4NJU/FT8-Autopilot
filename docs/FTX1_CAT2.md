# Contrôle radio Hamlib et sécurité FTX-1

## Scope

Radio control and meter readings use a local Hamlib `rigctld` connection. The
operator selects the radio model, CAT port, and baud rate. AutoPilot validates
an already-running `rigctld` against that CAT port and baud rate before reusing
it, so a stale daemon cannot silently control another configured radio.

The FTX-1-specific tuner path is enabled only after Hamlib reports the real
FTX-1 CAT identity. Other radios remain on generic Hamlib commands. AutoPilot
never changes USB MOD GAIN.

## Band profiles

Profiles contain only the WSJT-X `tx_audio_attenuation` used for each band.
The old `usb_mod_gain` settings field is read only for compatibility with
existing configuration files and is never applied to the radio.
Use SAVE CURRENT BAND, DELETE CURRENT BAND PROFILE, RESET ALL BAND PROFILES,
and AUTO APPLY BAND PROFILES to manage them. If a forced hop cannot restore a
profile, automatic TX stays blocked until an explicit operator ARM. A failed
radio frequency command does not discard the calls already heard.

During a transmission, AutoPilot ignores the first 1.5 seconds, then samples
Hamlib ALC, SWR, and RF power. At the end of the transmit period it adjusts the
WSJT-X audio attenuation one small step in either direction to keep ALC inside
the configured window. Samples without RF power are excluded from ALC learning.

SWR protection remains active during the transmit period. Only readings with
measurable RF power are considered valid SWR samples. Consecutive valid
readings at or above the configured threshold send Halt Tx immediately and
wait for a confirmed return to RX. AutoPilot then starts one Hamlib tuner cycle. On the
FTX-1, CAT state `AC..3` confirms that tuning really started and the transition
back to `AC..1` confirms that the tuner finished. The radio temporarily selects
5 W and restores its configured power itself; AutoPilot does not write a power
value during this sequence.

The last two RF-valid SWR samples must both be below the configured limit. A
confirmed failed match excludes only the current band for the session and tries
the next allowed band. Missing tuner confirmation or insufficient meter data
stops and disarms AutoPilot without excluding the band.

When SWR protection is enabled, AutoPilot refuses to arm if Hamlib cannot
identify the configured radio. This avoids presenting an armed state while the
antenna protection is unavailable.
