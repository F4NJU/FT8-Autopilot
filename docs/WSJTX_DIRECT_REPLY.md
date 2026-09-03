# WSJT-X AP1 Direct Reply

FT8-AutoPilot uses the standard WSJT-X UDP Reply message for a selected CQ,
QRZ, or valid direct caller. The AP1 WSJT-X fork extends WSJT-X acceptance of
direct caller Reply packets while retaining the native WSJT-X Reply path.

The exact Decode fields are sent back to WSJT-X. WSJT-X performs its normal
Reply handling, including TX audio DF selection and Hold Tx Freq behavior.
Autopilot does not modify or validate TX DF before or after Reply.

## AP1 wire IDs

```text
18 retired/reserved
19 SetTxPeriod
20 SetDialFrequency
21 SetTxAudioAttenuation
22 QueryTxAudioAttenuation
23 TxAudioAttenuationState
```

Type 18 has no active implementation and must not be reused. The fork source,
build instructions, and source archive are maintained in the companion
`WSJTX-AutoPilot-AP1` repository.
