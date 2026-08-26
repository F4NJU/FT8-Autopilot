# FT8-Autopilot
WIP Windows companion for WSJT-X that automates FT8/FT4 station selection, CQ/direct-call replies, QSO sequencing support, duplicate filtering, DXCC/continent priorities and safety watchdogs. Experimental software for supervised amateur-radio operation.
# FT8-AutoPilot

> [!WARNING]
> **WORK IN PROGRESS — EXPERIMENTAL SOFTWARE**
>
> FT8-AutoPilot can automatically initiate amateur-radio transmissions.
> The station operator remains responsible for all transmissions and for
> compliance with applicable regulations.

FT8-AutoPilot is a Windows companion for WSJT-X providing automatic
FT8/FT4 station selection, CQ and direct-call handling, duplicate filtering,
DXCC/continent priorities and QSO safety watchdogs.

## Status

🚧 **WIP / Experimental**

This project is under active development and interfaces with a modified
WSJT-X build for some advanced functions.

## License

GPL-3.0-or-later


FT8-AutoPilot is an experimental Windows companion for WSJT-X, designed to automate FT8/FT4 station selection and QSO handling while keeping WSJT-X responsible for the actual digital-mode sequencing.

The project is currently WIP and interfaces with a modified WSJT-X build for some advanced control functions.

Development status

Legend:

Status	Meaning
✅ Working	Implemented and tested in real operation
🧪 Experimental	Implemented but still being validated
🚧 In development	Currently being implemented/debugged
📋 Planned	Intended feature, not functional yet
Feature status
Core WSJT-X communication
Feature	Status
WSJT-X UDP listener	✅ Working
Heartbeat / Status / Decode parsing	✅ Working
FT8 support	✅ Working
FT4 protocol support	🧪 Experimental
CQ detection	✅ Working
Real WSJT-X UDP Reply	✅ Working
WSJT-X QSOLogged handling	✅ Working
Multiple successive QSOs	✅ Working
GUI Windows	🧪 Experimental
PyInstaller Windows build	🧪 Experimental
Automatic CQ selection

Status: ✅ Working / continuously improving

FT8-AutoPilot analyses CQ messages received from WSJT-X and automatically selects eligible stations.

Example:

CQ DL3DLP JO33

Candidate selection can take into account filtering and scoring rules.

The selection engine is separate from the GUI and WSJT-X control layer.

Direct-call support

Status: ✅ Working with modified WSJT-X

FT8-AutoPilot can detect stations explicitly calling the local station:

F4NJU YO6LM KN25
F4NJU YO6LM -10

Direct calls can take priority over ordinary CQ candidates.

This currently requires a small modification to WSJT-X because the standard
WSJT-X network Reply command normally only permits CQ/QRZ decodes.

QSO lifecycle tracking

Status: ✅ Working / 🧪 being refined

Typical state flow:

IDLE
  ↓
Candidate selected
  ↓
Reply sent
  ↓
CALLING
  ↓
QSO ACTIVE
  ↓
WSJT-X Auto Seq
  ↓
QSOLogged
  ↓
COMPLETE
  ↓
IDLE

FT8-AutoPilot can continue automatically with another candidate after a QSO
has completed.

Recovery logic for unusual FT8 sequences is still being refined.

Worked Today / duplicate filtering

Status: 🧪 Experimental / being integrated

The intended duplicate policy is:

UTC day + callsign + band

Example:

YO6LM worked today on 20 m
→ blocked on 20 m

YO6LM worked today on 20 m
→ still eligible on 40 m

A station must only become Worked Today after a confirmed/logged QSO.

The following must NOT count as worked:

Reply sent;
unanswered call;
watchdog timeout;
aborted QSO;
failed sequence.

QSOLogged is intended to be the primary confirmation source.

Persistent Worked database

Status: 🚧 In development

Planned storage:

%LOCALAPPDATA%\FT8-AutoPilot\autopilot.sqlite3

The SQLite database will store confirmed QSOs used by duplicate filtering.

The database is intended to survive application updates and restarts.

Wavelog / ADIF synchronization

Status: 📋 Planned

Automatic synchronization with a local ADIF file is planned.

The objective is to import existing QSOs at application startup so FT8-AutoPilot
already knows which stations have been worked that day.

Planned behaviour:

ADIF
  ↓
CALL / DATE / BAND / MODE
  ↓
Worked database

Import should be idempotent and configurable through the GUI.

This feature should not currently be assumed to be functional unless explicitly
reported by the application.

Candidate priorities

Status: 🚧 In development

The candidate engine is being extended to support operator preferences.

Planned / developing criteria include:

DXCC entity;
continent;
signal level;
direct calls;
POTA;
SOTA;
QRP;
user blacklist;
temporary ignore;
Worked Today.

The design separates:

Blocking rules

Example:

Worked Today
Blacklist
Directed CQ not intended for us
Stale decode
Low-confidence decode

from:

Priority bonuses

Example:

Direct Call          high priority
Preferred DXCC       bonus
Preferred continent  bonus
POTA / SOTA / QRP    optional bonus
Signal level         small bonus

Exact scoring values are still subject to change.

DXCC / country / continent information

Status: 🚧 In development

The GUI is intended to display:

Callsign
Country
DXCC entity
Continent
Locator
SNR

An offline DXCC/prefix resolver is planned so normal operation does not depend
on Internet access.

Directed CQ support

Status: 🚧 In development / experimental

FT8-AutoPilot is being extended to distinguish general CQ calls from directed
CQ calls such as:

CQ EU DL1ABC JO40
CQ OC VK3ABC QF22
CQ JA JA1ABC PM95

The local station should only automatically answer a directed CQ if it matches
the requested criterion.

Unknown directed CQ formats should be handled conservatively.

POTA / SOTA / QRP awareness

Status: 📋 Planned / partially under development

The candidate model is intended to expose activity information such as:

POTA
SOTA
QRP

This information may later be used for filtering or priority scoring.

Important:

/P ≠ automatically POTA

and a weak signal must never automatically be assumed to mean QRP.

Detecting a remote station working someone else

Status: 🧪 Experimental

FT8-AutoPilot can detect situations where the currently selected station
clearly begins communicating with another station.

Example:

Current remote:
II7MGXX

Decode:
UT2UB II7MGXX +13

The intended behaviour is:

abort current attempt
→ temporary cooldown for II7MGXX only
→ immediately search for another candidate

This logic is still being tuned to avoid unnecessary dead periods.

Remote returning to CQ

Status: 🧪 Experimental

A remote station transmitting CQ again does not necessarily mean the QSO should
be abandoned immediately.

The current development goal is to tolerate a limited number of repeated CQs
before considering the attempt lost.

This avoids loops such as:

call station
→ remote CQ
→ abort
→ immediately select same station again
QSO progress watchdog

Status: 🚧 In development

A normal time-based watchdog is not sufficient when the remote station keeps
replying without advancing the FT8 exchange.

Example:

F4NJU M9VVE IO93
F4NJU M9VVE IO93
F4NJU M9VVE IO93
...

The planned semantic watchdog tracks stages such as:

CALL / GRID
REPORT
R-REPORT
RRR / RR73
73

Repeated messages at the same protocol stage should eventually cause the QSO
to be aborted even if packets continue to arrive.

Final 73 / RRR retry handling

Status: 🚧 In development

Example:

F4NJU UI6O RRR
UI6O F4NJU 73

...

F4NJU UI6O RRR

The remote station may not have received the final 73.

FT8-AutoPilot is being developed to maintain a short finalization window and
retransmit the terminal response when appropriate.

Planned safeguards include:

one short RX grace period;
limited retry count;
no duplicate QSO entry;
no interruption of a newer QSO that has already genuinely progressed.
Smart TX Frequency

Status: 📋 Planned

An experimental Smart TX Frequency feature is planned.

The idea is to use recent WSJT-X Decode frequencies to estimate occupied areas
inside the FT8 passband.

Example:

Remote DF: 895 Hz

Estimated free slot:
1650 Hz

RX remote: 895 Hz
TX:        1650 Hz

If no suitable free area exists:

TX = remote frequency

This would be a Decode-based occupancy estimate, not a real spectrum
analyser or FFT.

Implementation will require additional WSJT-X control because the standard UDP
API does not provide all of the required TX-frequency control.

Windows GUI

Status: 🧪 Experimental

FT8-AutoPilot includes a native Windows GUI based on PySide6.

Current/developing interface elements include:

WSJT-X connection state;
current band;
current mode;
Armed / Disarmed state;
candidate station;
active station;
direct-call indication;
QSO state;
activity log.

Planned additions include:

country / DXCC / continent;
candidate score explanation;
Worked Today status;
POTA/SOTA/QRP information;
watchdog counters;
blacklist management;
DXCC preferences;
continent preferences;
session statistics;
update management.

The GUI is still under active development.

Arm / Disarm safety model

Status: 🧪 Experimental

The intended safety model is:

application startup
→ DISARMED

The operator must explicitly enable automatic operation.

Disarming should stop new automatic initiations without unnecessarily
breaking a QSO already being completed by WSJT-X Auto Seq.

Halt TX integration

Status: 🚧 In development

FT8-AutoPilot is intended to use WSJT-X network control to stop TX when a
watchdog detects a genuinely stalled or invalid QSO.

No GUI mouse automation should be required.

Local Windows data paths

Status: 📋 Planned / migration in progress

Target locations:

%APPDATA%\FT8-AutoPilot\

for configuration, and:

%LOCALAPPDATA%\FT8-AutoPilot\

for:

database;
logs;
runtime data.

Persistent user data should never depend on the installation directory.

Automatic updates

Status: 🚧 Infrastructure working / application updater planned

GitHub Actions currently builds WIP Windows releases automatically.

Development tags use:

v0.1.0-wip.1
v0.1.0-wip.2
v0.1.0-wip.3

The GitHub pipeline can:

run tests
→ build Windows application
→ create ZIP
→ publish GitHub prerelease

Planned application-side update features:

check for updates;
Stable / WIP update channel;
update notification in GUI;
download release;
automatic replacement of program files;
restart FT8-AutoPilot.

The in-application updater is not yet functional.

GitHub automated builds

Status: ✅ Working

WIP releases are automatically built using:

GitHub Actions;
Python;
PyInstaller;
Windows runners.

Published WIP versions are marked as prereleases.

WSJT-X patch

Status: 🧪 Experimental

Some advanced features require a modified WSJT-X build.

Currently this includes direct-reply functionality not available through the
standard WSJT-X Reply command.

Future features such as Smart TX Frequency may require additional minimal
extensions.

The patch should remain isolated and documented separately.

Future ideas

Status: 📋 Planned / exploratory

Possible future additions include:

new DXCC priority;
new grid priority;
band-specific priorities;
DXCC wanted list;
continent wanted list;
advanced blacklist/whitelist;
POTA/SOTA prioritisation;
Wavelog integration;
automatic ADIF synchronization;
integrated updater;
richer session statistics;
candidate history;
optional real spectrum occupancy detection;
additional WSJT-X safety controls.

These are not commitments and should not be considered functional features.

Current focus

The current development priorities are:

1. Reliable automatic QSO lifecycle
2. Correct Direct Call handling
3. No unnecessary duplicate QSOs
4. Efficient recovery after failed attempts
5. Robust watchdog behaviour
6. Candidate filtering and scoring
7. Stable Windows GUI
8. Safe unattended-assistance controls
Legal & safety notice

FT8-AutoPilot is experimental software intended for licensed amateur-radio
operators.

The station operator remains responsible for all transmissions made by their
station and for compliance with:

licence conditions;
applicable national regulations;
band plans;
power limits;
identification rules;
interference requirements.

Do not rely on FT8-AutoPilot as a safety system.

Software bugs, decoding errors, WSJT-X behaviour, network failures or operating
system failures may result in unintended transmissions.

The software is provided as is, without warranty.

Licence

GNU General Public License v3.0

See LICENSE.

Project summary
Platform:        Windows
Language:        Python
GUI:             PySide6
WSJT-X control:  UDP
Packaging:       PyInstaller
Database:        SQLite (planned/in integration)
Modes:           FT8 / FT4
Status:          WIP / Experimental
Licence:         GPL-3.0

Do not assume that a feature mentioned in this README is available unless
it is marked ✅ Working.

## Bug reports

When reporting an operating issue, please include a diagnostic log or
use **Settings → Diagnostics → Export diagnostic**.

Diagnostic logs contain FT8 messages, callsigns, frequencies and QSO
state information, but should never contain passwords or API tokens.
