# MultiFlo touchscreen dashboard

This directory contains the first touchscreen application for the base BioTek
MultiFlo. The dashboard and Python bridge run together on a Raspberry Pi. A
browser on the attached display calls the bridge on `localhost`; the bridge
calls the existing, safety-guarded `multiflo` driver through the stable device
link `/dev/serial/by-id/usb-BTI_MultiFlo_14071419-if00-port0`.

The current dashboard supports:

- a single-screen **Dispense** workspace: plate choice, per-well volume, and a
  direct column map;
- 96-well, 96-deep-well, standard 384-well, and 384-deep-well layouts, with 384-well row
  band (odd/even) selection;
- flow rate, pre-dispense, and X/Y/Z offset behind an **Advanced settings**
  disclosure;
- three guided procedures derived from the cassette dead volume - **Load
  reagent**, **Change reagent**, and **End dispensing**;
- a **Protocol** builder and runner whose protocols are saved on the Pi;
- a **Quick** panel for one-off dispense, prime, purge, and shake; and
- a **Settings** panel with persistent Plate Geometry defaults for every
  dashboard-supported plate type and automatic maintenance-prime settings; and
- persistent cassette-volume and plate-count accounting.

The free-form wash editor and the press-and-hold manual prime/purge panel were
removed from the touchscreen. Prime and purge reach the instrument through the
guided procedures and through saved protocols; shake and soak are reachable only
as protocol steps.

## Target system

| Item | Current target |
| --- | --- |
| Hostname | `multiflo-display` |
| Hardware | Raspberry Pi 4 Model B Rev 1.5 |
| OS | Debian 13 (`aarch64`) |
| User | `multiflo-display` |
| Direct Ethernet | `169.254.251.153/16` on `eth0` |
| Wi-Fi | DHCP; currently `172.25.81.19/24` |
| Deployment root | `/home/multiflo-display/multiflo-app` |
| Serial device | `/dev/serial/by-id/usb-BTI_MultiFlo_14071419-if00-port0` |
| HTTP listener | `127.0.0.1:8000` (Pi loopback only) |

The direct Ethernet address is persistent in NetworkManager profile
`netplan-eth0`, has no gateway or DNS, and is marked `never-default`. Wi-Fi
remains DHCP-managed and provides the Pi's only default route.

## Handoff: moving the Pi to the instrument

Before unplugging, confirm nothing is mid-run and shut down cleanly:

```bash
curl -s http://127.0.0.1:8000/v1/health          # active_run_id must be null
ls demo/.multiflo-active-run.json                # must not exist
sudo systemctl poweroff
```

If either check fails, resolve it first - never move the Pi with a run active or
a retained crash marker.

Reconnecting at the instrument:

1. power the MultiFlo first, then the Pi;
2. plug the USB serial adapter into the Pi and confirm the stable
   `/dev/serial/by-id/usb-BTI_MultiFlo_14071419-if00-port0` link resolves (the
   underlying `/dev/ttyUSB*` number may change after a reconnect);
3. the bridge, kiosk, landscape transform, Ethernet, and Wi-Fi all restart on
   their own - nothing needs to be launched by hand;
4. on the touchscreen press **Refresh** and accept the instrument only if it
   reports serial `14071419`, the fitted cassette, and `ready`;
5. if the status row shows **Reconcile**, clear it only
   after confirming the instrument is physically stationary; and
6. review tubing, liquid, waste, cassette, plate, pump cover, and carrier path
   before the first motion.

If the dashboard is blank or stale, restart it over SSH:

```bash
systemctl --user restart multiflo-dashboard.service
systemctl --user restart multiflo-dashboard-kiosk-session.service
```

Nothing needs redeploying: `widget.html`, `bridge_server.py`, and `kiosk.sh` on
the Pi already match this repository, and the protocol library, cassette
ledger, and crash marker all survive the power cycle.

## Display target

The official 7-inch Raspberry Pi Touch Display 2 has a native 720 x 1280
portrait panel. This application targets the display rotated to **1280 x 720
landscape**.

At that viewport the page is locked to the screen:

- the status row starts at the top edge; there is no separate branding header;
- one compact row holds instrument identity/state, the instrument-detected
  cassette, the four-quarter lifetime indicator, the plate count, and Refresh;
- there is no manual cassette selector: the detected cassette is the sole source
  for workspace limits, plate support, protocol storage, and motion payloads;
- the editor workspace occupies all remaining height beneath that status row;
- the dispense panel and the three guided-procedure panels are full-width cards
  with their run controls pinned to the bottom edge; the protocol panel splits
  into a step list and a step editor; Settings separates Plate Geometry into a
  category list and editor;
- the column map fills the available panel width: 12 wide targets for the
  96-well plates, 24 x 2 for both 384-well types;
- touch controls retain practical minimum heights; and
- only the dispense card body when Advanced settings is open, the
  guided-procedure step bodies, the protocol step list and step editor, and the
  optional activity log have contained overflow. The browser page itself does
  not scroll.

The ordinary responsive layout remains available for laptop testing and narrow
portrait browser windows.

## Entering numbers

The Pi has no system keyboard, so tapping any number field opens a built-in
keypad instead of a text cursor. It reads the field's own `min`, `max`, and
`step`, shows the allowed range, rejects a value outside it, and only enables
**Set** when the entry is valid - a 5 uL cassette volume of 253 uL/well is
refused because that cassette works in 5 uL increments. The +/- key appears
only on fields that accept negative values, such as the X and Y offsets.

A physical keyboard still works while the keypad is open: digits type,
Backspace deletes, Enter sets, Escape cancels. The +/- stepper beside each
field is unchanged.

Text fields (protocol and step names, the wait message) still need a physical
keyboard.

## Dispense workspace

The dispense panel is the dashboard's main screen and maps one-to-one onto
`POST /v1/operations/dispense`:

| Control | Request field |
| --- | --- |
| Plate segmented buttons | `plate_type` (options follow the detected cassette) |
| Volume stepper | `volume_ul` (stepped in full cassette increments) |
| Column squares and numbers | `columns`; `"all"` when every column is on |
| Row 1 / Row 2 band buttons (384-well only) | `row_sections`: `all`, `odd`, `even` |
| Advanced settings | `flow_rate`, `pre_dispense_volume_ul`, `pre_dispense_cycles`, `x_offset_steps`, `y_offset_steps`, `dispense_height_steps` |

X, Y, and Z in Advanced settings are per-dispense offsets from the persistent
Plate Geometry defaults. Zero uses the configured plate position. The dashboard
resolves them to explicit motion values before submission; X must resolve to
-60 through +60 steps, Y to -40 through +40, and absolute Z to 100-1100.
Explicit geometry stored in an existing protocol remains authoritative.

## Plate Geometry settings

**Settings > Plate Geometry** edits the default X offset, Y offset, and absolute
dispense Z height independently for 96-well, 96-deep-well, 384-well, and
384-deep-well plates. Saving writes validated values atomically on the Pi. The
bridge also applies these defaults to API dispense requests that omit geometry,
so dashboard and maintenance callers have the same behavior. Explicit request
values are never replaced.

**Restore built-in values** loads the codec defaults into the editor; **Save
defaults** persists them and clears the corresponding overrides. The built-in
codec values remain the fallback if the settings file is absent or invalid.

A 96-well or deep-well plate shows one band of 12 squares for rows A-H. A
Either 384-well plate shows two bands of 24: Row 1 is the odd rows (A, C, E, ...) and
Row 2 the even rows. Tapping a square or its column number toggles that column
in both bands; tapping a band label drops that band. At least one band must stay
selected. Deselecting every column is allowed in the editor but disables Run.

The line above the run button restates the resolved plan - plate, per-well
volume, well count, and the total millilitres including pre-dispense - which is
the same figure recorded in the cassette ledger.

The top cassette summary shows a persistent plate count beside Lifetime. It is
the number of unique completed runs containing a dispense operation since that
cassette was last marked Replaced. Multiple dispense steps in one protocol count
once; prime and purge maintenance runs do not count.

Only the odd-row 384-well dispense has hardware evidence. The even band and
partial 384-well column maps are fixture-verified from calib30 to calib32 and
are encoded by the same packet path.

## Automatic maintenance prime

**Settings > Maintenance** shows the next automatic prime and edits its
per-well volume for the fitted cassette. The built-in volumes are 200 uL/well
for a 5 uL cassette and 50 uL/well for a 1 uL cassette. Speed is fixed to low.

The bridge owns the two-hour timer. Every confirmed completed Prime or Dispense
resets it, including protocol and Quick operations. Purge and non-liquid steps
do not reset it. One minute before the due time, the dashboard shows **Auto
prime. Wait 1 min**. At the due time the bridge starts only after a fresh check
of the serial, fitted cassette, Ready state, reconciliation state, and active
run. If the instrument is not safe and idle, the prime waits.

The timer and volumes persist in:

```text
/home/multiflo-display/multiflo-app/demo/auto_prime_settings.json
```

The instrument cannot detect the liquid at the tubing ends. The tubing must
remain in clean water whenever the idle auto-prime feature is in service.

## Guided procedures

Three panels share one wizard. The operator works left to right: a step unlocks
only once the previous one has completed, and any step that needs the tubing
moved requires an explicit confirmation before Run is enabled. A completed step
stays selectable so it can be repeated, and finishing the last step shows the
resulting system state.

Prime and purge volumes are **per well**: the driver applies the value to each
of the 8 cassette channels, so 1575 uL/well moves 12.6 mL in total. Every
volume in the dashboard is labelled `uL/well` for that reason.

Each step is one command with one per-well volume, editable in Method settings.
The cassette dead volumes are 4.2 mL (5 uL) and 1.2 mL (1 uL) for reference;
nothing is derived from them.

| Step | 5 uL | 1 uL |
| --- | --- | --- |
| Return reagent | 1575 uL/well | 450 uL/well |
| Wash with water | 3000 uL/well | 900 uL/well |
| Empty the lines | 1610 uL/well | 480 uL/well |
| Load new reagent, Load reagent, Push out the water | 1575 uL/well | 450 uL/well |

### Method settings

Each procedure panel has a **Method settings** button, at the top right under
the spec line. It edits one thing: the per-well volume of each step of that
procedure, for the fitted cassette. Nothing is shared between procedures and
nothing is calculated from the dead volume. The modal shows the resulting
total through the 8 wells as you type.

**Apply** uses the values for this session only. Ticking **Save as the default
on this instrument** also writes them to the Pi:

```text
/home/multiflo-display/multiflo-app/demo/procedure_settings.json
```

**Restore built-in values** puts the table below back in the fields; it takes
effect when Apply is pressed, and saving that state clears the stored
overrides. Only values that differ from the built-ins are stored, and the
button reads `Method settings ·` while that procedure is overridden. The bridge
validates every volume (1-3000 uL/well, the driver's own per-command limit)
before storing it, and ignores a stale settings file rather than refusing to
start.

### Load reagent

Run before a dispensing session, starting from a system left standing in water.
Ends with the lines full of reagent.

| Step | Operator action | Motion | 5 uL (uL/well) | 1 uL (uL/well) |
| --- | --- | --- | --- | --- |
| 1. Push out the water | Leave the tubing in the water bottle | Prime, pushing the standing water to waste | 1575 | 450 |
| 2. Load reagent | Confirm the tubing is in the reagent bottle | Prime the reagent into the lines | 1575 | 450 |

### Change reagent

Run to swap one reagent for another. Ends with the lines full of the new
reagent.

| Step | Operator action | Motion | 5 uL (uL/well) | 1 uL (uL/well) |
| --- | --- | --- | --- | --- |
| 1. Return reagent | Leave the tubing in the current bottle | Purge back to the bottle | 1575 | 450 |
| 2. Wash with water | Confirm the tubing is in the water bottle | Prime water through the lines | 3000 | 900 |
| 3. Empty the lines | Confirm the tubing is out of the liquid | Prime air through the lines | 1610 | 480 |
| 4. Load new reagent | Confirm the tubing is in the new bottle | Prime the new reagent | 1575 | 450 |

### End dispensing

Run after a dispensing session. Its two steps are the same return and wash used
by Change reagent, and it deliberately stops there: the system is left standing
in water, which is the state Load reagent expects.

| Step | Operator action | Motion | 5 uL (uL/well) | 1 uL (uL/well) |
| --- | --- | --- | --- | --- |
| 1. Return reagent | Leave the tubing in the current bottle | Purge back to the bottle | 1575 | 450 |
| 2. Wash with water | Confirm the tubing is in the water bottle | Prime water through the lines | 3000 | 900 |

Each step is one command, so it is recorded in the cassette ledger on its own.
At the built-in volumes the procedures move 25.2 / 61.5 / 36.6 mL on a 5 uL
cassette and 7.2 / 18.6 / 10.8 mL on a 1 uL cassette through the eight wells.

Unlike a dispense, prime and purge are not restricted to the cassette's
per-well dispense range: the models and the driver preflight allow 1-3000 uL
for either operation on any cassette, which is the only cap on a step volume.

## Protocols

The **Protocol** panel builds, stores, and runs multi-step protocols. The step
list is on the left, the editor for the selected step on the right. Each step
carries an optional name, exposes the same kind of basic controls the Dispense
panel uses, and hides the rest behind its own **Advanced settings**
disclosure.

| Step | Basic | Advanced |
| --- | --- | --- |
| Dispense | plate, volume per well, column map | flow rate, pre-dispense volume and cycles, X/Y offset |
| Prime, Purge | volume per channel | flow rate |
| Shake, Soak | duration | plate, carrier position |
| Wait | a set time, or until confirmed | message shown while waiting |
| Repeat | how many times the block runs in total | - |

Dispense, Shake, and Soak are the plate-interacting steps and must resolve to
one plate type within a protocol. The dashboard derives that type before save
or run and rejects a mixed-plate protocol. Prime and Purge have no plate
interaction and therefore expose no plate control; their required `plate_type`
field inherits the protocol plate, or uses `96_well` as a harmless placeholder
in a Prime/Purge-only protocol. Shake and Soak offer all four plate types
independently of the fitted cassette; cassette filtering applies only to
Dispense.

### Wait and Repeat

Wait and Repeat are sequenced by the dashboard, not by the instrument. They
appear as ordinary steps because that is what they are to the operator; the
driver never receives them.

- **Wait** either counts down a fixed number of seconds and continues on its
  own, or holds the protocol until Continue is pressed on the run bar. Its
  message is shown on screen while it waits.
- **Repeat** re-runs every step above it, back to the previous Repeat or the
  start of the protocol. The count is the number of times that block runs in
  total, and the step list shows the expanded count ("4 steps - 12 to run").

A protocol expanding past 400 executed steps is refused before any motion.

### Execution

The dashboard walks the expanded list and submits each driver step on its own
through the existing operation endpoints, so every step is preflighted and
recorded exactly as a single-step action is. The step in progress is
highlighted in the list, and the run bar reports `step 4 of 12 - cycle 2 of 3`.
Abort stops the sequence: it aborts the running step cooperatively and cancels
a pending Wait.

### Storage

Protocols and operator-selected defaults are saved beside the bridge:

```text
/home/multiflo-display/multiflo-app/demo/dashboard_protocols.json
/home/multiflo-display/multiflo-app/demo/procedure_settings.json
/home/multiflo-display/multiflo-app/demo/plate_geometry.json
```

It is written atomically (temporary file, fsync, rename) and is not touched by
deployment. Every step is validated against the production models before it is
stored, so an unrunnable dispense cannot be saved. A stored dispense keeps
`cassette_type: "any"`; the fitted cassette is stamped on at run time, which
keeps a protocol portable between cassettes and leaves the volume check to
preflight.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/protocols` | list the stored protocols |
| `POST` | `/v1/protocols` | create, or replace by `id` |
| `DELETE` | `/v1/protocols/{id}` | remove one protocol |

The library holds up to 100 protocols of up to 50 steps each.

## Quick

The Quick panel runs a single operation now, saving nothing. Pick
**Dispense**, **Prime**, **Purge**, or **Shake**, set the basic controls, and
press Run.

It shares the protocol builder's step editor, so each operation exposes exactly
the same basic fields and hides the same options behind **Advanced settings**;
the quick panel simply drops the step-name field. Each operation keeps its own
settings while the page is open, so switching between them does not lose what
was typed. The line above the run button restates the resolved action - well
count and total millilitres for a dispense, the per-well and total volume for a
prime or purge.

Each run goes to the same `/v1/operations/*` route a Dispense-panel run uses,
so it is preflighted, deduplicated, and recorded in the cassette ledger
identically.

## Architecture

```text
Touch Display browser
  http://127.0.0.1:8000/
          |
          | HTTP, same Raspberry Pi
          v
stdlib dashboard bridge
          |
          v
ProtocolRunner -> MultiFloDriver -> SerialByteTransport -> /dev/serial/by-id/...
                                                              |
                                                              v
                                                         BioTek MultiFlo

Laptop maintenance path
  Ethernet 3 -> SSH multiflo-display@169.254.251.153
             -> authenticated tunnel to Pi localhost:8000
```

The bridge serves `widget.html` and a small JSON API from the same origin, so no
separate frontend server or CORS configuration is required. It deliberately
listens only on Pi loopback because the API includes hardware-motion endpoints;
it is not exposed directly on Wi-Fi or Ethernet.

## API surface

- `GET /` - touchscreen dashboard
- `GET /v1/health`
- `GET /v1/device` - read-only identity, modules, and state
- `GET /v1/cassette-usage` - persistent totals and recent ledger events
- `POST /v1/reconcile` - guarded startup-marker reconciliation
- `POST /v1/system/shutdown` - confirmed, idle-only Raspberry Pi shutdown
- `POST /v1/cassette-usage/{cassette}/reset`
- `POST /v1/operations/dispense`
- `POST /v1/operations/prime`
- `POST /v1/operations/purge`
- `POST /v1/operations/shake`
- `POST /v1/operations/soak`
- `POST /v1/protocols/validate`
- `GET /v1/procedure-settings` - stored guided-procedure overrides
- `POST /v1/procedure-settings` - replace the guided-procedure overrides
- `GET /v1/plate-geometry` - effective and built-in plate geometry defaults
- `POST /v1/plate-geometry` - replace persistent plate geometry overrides
- `GET /v1/maintenance/auto-prime` - schedule, state, and volume settings
- `POST /v1/maintenance/auto-prime` - replace auto-prime volumes
- `GET /v1/protocols` - stored protocol library
- `POST /v1/protocols` - create or replace one stored protocol
- `DELETE /v1/protocols/{id}`
- `POST /v1/runs` - multi-pulse guided-procedure steps and other ordered protocols
- `GET /v1/runs/{id}`
- `POST /v1/runs/{id}/abort`

## Raspberry Pi runtime prerequisite

The deployment launcher expects this interpreter to exist:

```text
/home/multiflo-display/multiflo-app/.venv/bin/python
```

The environment needs Pydantic and pyserial. On a fresh Pi, create it before the
first deployment (install `python3-venv` through the OS package manager first if
the `venv` module is unavailable):

```bash
mkdir -p /home/multiflo-display/multiflo-app
python3 -m venv /home/multiflo-display/multiflo-app/.venv
/home/multiflo-display/multiflo-app/.venv/bin/python -m pip install \
  'pydantic>=2,<3' pyserial
```

The `multiflo-display` account is already a member of `dialout` and `plugdev`,
which provide access to the USB serial device resolved by the stable link.

The top-right power button uses one command-specific sudo rule; it does not grant
the dashboard unrestricted root access. Install and validate it once on the Pi:

```bash
sudo visudo -cf \
  /home/multiflo-display/multiflo-app/demo/multiflo-dashboard-shutdown.sudoers
sudo install -m 0440 \
  /home/multiflo-display/multiflo-app/demo/multiflo-dashboard-shutdown.sudoers \
  /etc/sudoers.d/multiflo-dashboard-shutdown
```

The permitted command is exactly `/usr/sbin/shutdown now`. The API rejects the
request unless the operator explicitly confirms it and no run is active.

## Deploy and run

From the `multiflo` repository root:

```powershell
python implementation_demo/deploy_and_run.py `
  --identity-file C:\path\to\ssh-key
```

The defaults are the target host, user, deployment directory, and port in the
table above. They can be changed with command-line arguments or these variables:

- `MULTIFLO_PI_HOST`
- `MULTIFLO_PI_USER`
- `MULTIFLO_PI_REMOTE`
- `MULTIFLO_SSH_KEY`
- `MULTIFLO_SSH_PASSWORD` (optional; never stored in source)
- `MULTIFLO_HTTP_PORT`
- `MULTIFLO_SERIAL_PORT`

The launcher:

1. uploads the required modules from `src/multiflo`;
2. uploads the bridge, dashboard, kiosk launcher, and managed user service;
3. validates and installs the service, including its loopback listener and
   stable serial-device path;
4. restarts the managed service; and
5. waits for the loopback health endpoint to report ready.

After deployment, use:

```text
Touchscreen: http://127.0.0.1:8000/
Laptop:      http://127.0.0.1:8000/ after opening the SSH tunnel below
```

```powershell
ssh -i C:\Users\nej\.ssh\multiflo_display_ed25519 `
  -L 8000:127.0.0.1:8000 `
  multiflo-display@169.254.251.153
```

## Manual bridge start

```bash
cd /home/multiflo-display/multiflo-app
PYTHONPATH=/home/multiflo-display/multiflo-app \
  ./.venv/bin/python demo/bridge_server.py \
  --host 127.0.0.1 --http-port 8000
```

The crash marker and cassette ledger default to paths beside the deployed
bridge, independent of the Linux username:

```text
/home/multiflo-display/multiflo-app/demo/.multiflo-active-run.json
/home/multiflo-display/multiflo-app/demo/cassette_usage.jsonl
/home/multiflo-display/multiflo-app/demo/dashboard_protocols.json
```

Deployment does not overwrite any of them.

## Safety invariants

- Run and hold controls execute real motion. Confirm tubing, cassette, plate,
  liquid, waste, pump cover, and carrier clearance first.
- The driver preflights every run against product serial `14071419`, the fitted
  cassette, and the operation limits before motion.
- A repeated `request_id` does not start the same motion twice.
- Single-step and manual touchscreen actions call the explicit operation
  endpoints; the operator never enters a protocol name.
- Run dispense starts motion on one press. There is no separate confirmation
  toggle in front of it: the button is enabled only while the instrument
  reports Ready, the expected serial matches, a cassette is detected, and no
  run is active.
- A saved protocol is only as safe as its steps. Each one is validated against
  the production models when it is stored and preflighted again at run time,
  but the dashboard cannot know what is on the deck: review a protocol before
  running it on a loaded instrument.
- Every guided-procedure step that needs the tubing moved requires the operator
  to confirm the new position before that step can run. The confirmation is
  cleared again after the step completes and whenever another step is selected,
  so it can never carry over to a different tubing position.
- The dashboard is served with `Cache-Control: no-store`. The kiosk keeps a
  persistent Chromium profile, so without it a deployment could leave the
  operator looking at the previous dashboard while the bridge already ran the
  new one.
- Prime and purge are volume pulses. Releasing a hold prevents the next pulse;
  it cannot cancel a pulse the instrument already accepted.
- Abort is cooperative between steps. It is not an emergency stop.
- A crash or ambiguous transport result retains the active-run marker. Never
  blindly clear the marker or retry uncertain motion.
- Shutdown must not occur during an active or uncertain run.

## Cassette usage accounting

Only confirmed, completed liquid steps are recorded:

- 96-well dispense = per-well volume x selected columns x 8 rows;
- 384-well dispense = per-well volume x selected columns x 8 rows for a single
  row band (odd or even) or 16 rows for both;
- pre-dispense = configured volume x cycles x 8 cassette channels; and
- prime or purge = configured volume x 8 cassette channels, which also covers
  every pulse of the change-reagent procedure.

The `run_id:step_index` event ID prevents double-counting. A cassette
replacement requires operator confirmation, verifies the fitted cassette, and
records the previous total before resetting it.

## Current Pi configuration

Applied on 2026-09-01:

- laptop adapter `Ethernet 3` uses persistent `169.254.251.152/16` with DHCP
  disabled and no gateway or DNS;
- `eth0` uses persistent `169.254.251.153/16`, autoconnects, and cannot supply a
  default route; direct SSH was measured at 1-2 ms;
- Wi-Fi remains DHCP-managed at `172.25.81.19/24` and supplies the only default
  route;
- `DSI-1` is the connected Touch Display 2 and Kanshi persists transform `270`,
  producing a logical 1280 x 720 landscape desktop;
- the Goodix capacitive touchscreen is detected by the input stack;
- `multiflo-dashboard.service` is enabled under the `multiflo-display` user,
  uses the existing virtual environment, restarts on failure, and logs to the
  user journal;
- user lingering is enabled, so the bridge can start without an interactive
  login;
- the bridge is bound to `127.0.0.1:8000` and currently reports healthy;
- Chromium kiosk autostart is installed, bypasses the unused desktop keyring,
  and blocks idle blanking only while the kiosk process is active; and
- a captured compositor screenshot confirms the dashboard renders at 1280 x
  720 without page scrolling.

Later deployments on 2026-09-01 replaced the protocol editor with the dispense
workspace and the wash editor with the change-reagent procedure, restarted
`multiflo-dashboard.service`, and confirmed the served page matches the
repository copy. Compositor and headless screenshots at 1280 x 720 confirm that
the 96-well, 96-deep-well, and 384-well maps, the change-reagent steps for both
cassettes, all three guided procedures, and the busiest state (384-well with
Advanced settings open) all render without page scrolling.

A Pi reboot at 2026-09-01 12:29 CEST verified that persistent Ethernet, Wi-Fi,
the managed bridge, landscape transform, graphical kiosk autostart, the scoped
idle inhibitor, and Raspberry Pi Connect all recover automatically. Touch tests
against the top-right Refresh and bottom-left Activity log controls confirmed
that input coordinates rotate with the display.

On 2026-09-02 the bridge and managed service were changed from the volatile
`/dev/ttyUSB0` node to the serial-specific `/dev/serial/by-id/` link. Acceptance
was performed while that link resolved to `/dev/ttyUSB1`: the read-only device
probe returned serial `14071419`, controller `idle`, program state `ready`, and
the touchscreen showed **Instrument ready**. No motion endpoint was called.

## Remaining acceptance checks

Before motion testing:

1. exercise the remaining navigation, numeric fields, confirmation controls,
   on-screen keyboard, and activity overlay on the physical display;
2. confirm the MultiFlo USB serial link still resolves, then query only
   `/v1/health` and `/v1/device` through the local browser or authenticated SSH
   tunnel;
3. accept the device only if serial `14071419`, the expected allowlist, fitted
   module, and `program_step_state: ready` are reported; and
4. review tubing, liquid, waste, cassette, plate, pump cover, and carrier path
   before separately authorizing any motion test.

For maintenance, attach a keyboard and use `Alt+F4`, or terminate the scoped
kiosk Chromium process over SSH. Normal idle locking resumes when the kiosk is
not running.

## Files

| File | Role |
| --- | --- |
| `bridge_server.py` | Owns the driver and serves the dashboard/API on the Pi |
| `widget.html` | Touch-first dashboard, including the landscape kiosk layout |
| `deploy_and_run.py` | Secure laptop-to-Pi deployment launcher |
| `multiflo-dashboard.service` | Loopback-only managed bridge user service |
| `kanshi-touchscreen-landscape.conf` | Persistent DSI landscape profile |
| `kiosk.sh` | Keyring-free Chromium kiosk launcher with scoped idle inhibition |
| `multiflo-dashboard-kiosk.desktop` | Graphical-session kiosk autostart entry |
| `test_bridge.py` | Bridge and cassette-accounting tests |
| `SETUP.md` | Target architecture and operating notes |
