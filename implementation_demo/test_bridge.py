from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from multiflo.models import (
    CassetteType,
    PeristalticDispense,
    PeristalticPrime,
    PeristalticPurge,
    Shake,
)
from multiflo.runner import (
    ControllerState,
    RunState,
    RunStatus,
    StartResult,
    StepResult,
)

from implementation_demo import deploy_and_run
from implementation_demo.bridge_server import (
    AUTO_PRIME_DEFAULT_VOLUMES_UL,
    AUTO_PRIME_INTERVAL_SECONDS,
    AutoPrimeScheduler,
    AutoPrimeStore,
    BUILT_IN_PLATE_GEOMETRY,
    CassetteUsageStore,
    PlateGeometryStore,
    ProcedureSettingsStore,
    ProtocolLibrary,
    SHUTDOWN_COMMAND,
    validate_library_steps,
    validate_auto_prime_settings,
    validate_plate_geometry_settings,
    validate_procedure_settings,
    _Handler,
    ThreadingHTTPServer,
    pumped_volume_ul,
)


class DeploymentAndDisplayTests(unittest.TestCase):
    def test_384_deep_well_dashboard_geometry_uses_553_steps(self) -> None:
        self.assertEqual(
            BUILT_IN_PLATE_GEOMETRY["384_deep_well"],
            {
                "x_offset_steps": 0,
                "y_offset_steps": 0,
                "dispense_height_steps": 553,
            },
        )

    def test_deployment_defaults_match_the_touchscreen_pi(self) -> None:
        args = deploy_and_run._parser().parse_args([])
        self.assertEqual(args.host, "169.254.251.153")
        self.assertEqual(args.user, "multiflo-display")
        self.assertEqual(
            args.remote,
            "/home/multiflo-display/multiflo-app",
        )
        self.assertEqual(
            args.serial_port,
            "/dev/serial/by-id/usb-BTI_MultiFlo_14071419-if00-port0",
        )
        self.assertIsNone(args.identity_file)

    def test_deployment_reads_runtime_modules_from_src_layout(self) -> None:
        for filename in deploy_and_run.CORE_FILES:
            self.assertTrue((deploy_and_run.CORE_DIR / filename).is_file())

    def test_deployment_restarts_the_managed_user_service(self) -> None:
        command = deploy_and_run._service_restart_command()
        self.assertIn("systemctl --user daemon-reload", command)
        self.assertIn(
            "systemctl --user restart multiflo-dashboard.service",
            command,
        )
        self.assertIn("systemctl --user is-active --quiet", command)

    def test_deployment_requires_the_managed_user_service(self) -> None:
        command = deploy_and_run._service_check_command(
            deploy_and_run.PurePosixPath("/home/operator/app"),
            8123,
        )
        self.assertIn("systemctl --user cat multiflo-dashboard.service", command)
        self.assertIn("/home/operator/app/demo/bridge_server.py", command)
        self.assertIn("--host 127.0.0.1 --http-port 8123", command)
        self.assertIn("--serial-port /dev/serial/by-id/", command)

    def test_deployment_installs_the_versioned_managed_user_service(self) -> None:
        command = deploy_and_run._service_install_command(
            deploy_and_run.PurePosixPath("/home/operator/app"),
            "operator",
        )
        self.assertIn("systemd-analyze --user verify", command)
        self.assertIn("/home/operator/app/demo/multiflo-dashboard.service", command)
        self.assertIn(
            "/home/operator/.config/systemd/user/multiflo-dashboard.service",
            command,
        )
        self.assertIn("install -D -m 0644", command)

    def test_service_is_loopback_only(self) -> None:
        service = (Path(__file__).with_name("multiflo-dashboard.service")).read_text(
            encoding="utf-8"
        )
        self.assertIn("--host 127.0.0.1 --http-port 8000", service)
        self.assertIn(
            "--serial-port /dev/serial/by-id/usb-BTI_MultiFlo_14071419-if00-port0",
            service,
        )
        self.assertNotIn("/dev/ttyUSB0", service)
        self.assertNotIn("--host 0.0.0.0", service)

    def test_shutdown_permission_is_limited_to_the_fixed_command(self) -> None:
        rule = (Path(__file__).with_name("multiflo-dashboard-shutdown.sudoers")).read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            rule.strip(),
            "multiflo-display ALL=(root) NOPASSWD: /usr/sbin/shutdown now",
        )

    def test_dashboard_has_confirmed_power_button_next_to_refresh(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        action_cell = html.split('<div class="refresh-cell">', 1)[1].split("</div>", 1)[0]
        self.assertIn('id="refresh"', action_cell)
        self.assertIn('id="powerOff"', action_cell)
        self.assertIn("operator_confirmed_shutdown:true", html)
        self.assertIn("Wait until the screen goes dark", html)

    def test_kiosk_avoids_keyring_and_inhibits_idle_only_while_running(self) -> None:
        kiosk = (Path(__file__).with_name("kiosk.sh")).read_text(encoding="utf-8")
        self.assertIn("--password-store=basic", kiosk)
        self.assertIn("systemd-inhibit", kiosk)
        self.assertIn("--what=idle", kiosk)
        self.assertIn("--user-data-dir=", kiosk)
        self.assertIn("--password-store=basic", kiosk)

    def test_dashboard_has_touch_display_landscape_layout(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        self.assertIn("Raspberry Pi Touch Display 2: 1280x720", html)
        self.assertIn("html, body { width: 100%; height: 100%; overflow: hidden; }", html)
        self.assertIn("height: 100dvh;", html)
        self.assertIn('class="work-content"', html)
        self.assertNotIn("<header>", html)
        self.assertNotIn('id="connectionText"', html)

    def test_dashboard_uses_only_the_detected_cassette(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        self.assertNotIn('class="cassette-picker"', html)
        self.assertNotIn("data-cassette=", html)
        self.assertIn('const state = { cassette: null,', html)
        self.assertIn("applyDetectedCassette(fittedCassette)", html)
        self.assertIn("No cassette detected; cassette-dependent controls disabled", html)
        self.assertNotIn("selected cassette", html)

    def test_dashboard_uses_driver_1ul_volume_range(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        self.assertIn('{min: 1, max: 1200, step: 1}', html)
        self.assertIn('detected === "1ul" ? 1200 : 2500', html)
        self.assertIn('id="volume" type="number" value="10" min="1" max="1200"', html)

    def test_dashboard_replaces_the_protocol_editor_with_dispense(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        self.assertIn('id="panel-dispense"', html)
        self.assertIn('data-panel="dispense"', html)
        self.assertNotIn("Define protocol", html)
        self.assertNotIn('id="addStep"', html)
        self.assertNotIn('id="protocolSteps"', html)

    def test_dispense_panel_runs_without_a_confirmation_toggle(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        self.assertIn('id="runDispense"', html)
        self.assertNotIn('id="protocolConfirm"', html)
        self.assertIn("operator_confirmed_idle:true", html)

    def test_dispense_panel_hides_secondary_fields_behind_advanced(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        advanced = html.split('<details class="advanced"', 1)[1].split("</details>", 1)[0]
        for field in ('id="flowRate"', 'id="preVolume"', 'id="preCycles"', 'id="xOffset"', 'id="yOffset"', 'id="zOffset"'):
            self.assertIn(field, advanced)
        self.assertIn("Advanced settings", advanced)

    def test_z_offset_matches_xy_in_every_dispense_editor(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        self.assertEqual(html.count("Z offset (steps)"), 2)
        self.assertIn('dispense_height_steps: dispenseHeightFromOffset(', html)
        self.assertIn('height < 100 || height > 1100', html)
        self.assertIn('const FALLBACK_PLATE_GEOMETRY = {', html)
        self.assertIn('function geometryForPlate(plate)', html)
        self.assertIn('function is384Plate(plate)', html)
        self.assertIn('"384_deep_well":"384 deep-well"', html)
        self.assertIn('bindNumber(`${prefix}ZOffset`, "z_offset_steps")', html)
        self.assertIn('z_offset_steps: zOffsetFromHeight(body.plate_type, body.dispense_height_steps)', html)
        self.assertIn('serializeProtocolSteps(protocol.steps)', html)

    def test_workspace_uses_compact_plate_labels(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        self.assertIn(
            'function plateShortLabel(value) { return {"96_well":"96", '
            '"96_deep_well":"96DW", "384_well":"384", '
            '"384_deep_well":"384DW"}',
            html,
        )
        self.assertIn("button.textContent = plateShortLabel(plate)", html)
        self.assertIn("PLATE_GEOMETRY_ORDER.map(plate =>", html)
        self.assertIn(">${plateShortLabel(plate)}</option>", html)
        geometry_editor = html.split('id="geometryGrid"', 1)[1].split(
            "function readPlateGeometry", 1
        )[0]
        self.assertIn("plateLabel(plate)", geometry_editor)

    def test_guided_procedures_replace_the_wash_editor(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        for panel in ("load", "reagent", "end"):
            self.assertIn(f'data-panel="{panel}"', html)
        self.assertIn("section.id = `panel-${key}`", html)
        self.assertIn("const PROCEDURES = {", html)
        self.assertNotIn('id="panel-wash"', html)
        self.assertNotIn("Wash system", html)
        self.assertNotIn('id="addWashStep"', html)

    def test_each_procedure_step_carries_one_per_well_volume(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        # One editable per-well volume per step per cassette, nothing derived.
        self.assertIn("function procedureVolume(key, index)", html)
        self.assertIn("const MAX_STEP_VOLUME_UL = 3000;", html)
        self.assertNotIn("pulsesPerPass", html)
        self.assertNotIn("procedurePasses", html)
        for volumes in (
            'volumes: {"5ul": 1575, "1ul": 450}',
            'volumes: {"5ul": 3000, "1ul": 900}',
            'volumes: {"5ul": 1610, "1ul": 480}',
        ):
            self.assertIn(volumes, html)
        # Change reagent still reuses the return and water steps.
        reagent_plan = html.split("  reagent: {", 1)[1].split("  end: {", 1)[0]
        self.assertIn("RETURN_STEP,", reagent_plan)
        self.assertIn("WATER_STEP,", reagent_plan)

    def test_change_reagent_is_a_sequential_confirmed_wizard(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        self.assertIn("index > progress.completed", html)
        self.assertIn("done.completed += 1", html)
        reagent_plan = html.split("  reagent: {", 1)[1].split("  end: {", 1)[0]
        self.assertEqual(reagent_plan.count("confirm:"), 2)
        water_step = html.split("const WATER_STEP =", 1)[1].split("const PROCEDURES", 1)[0]
        self.assertIn("confirm:", water_step)
        self.assertIn(
            'if (plan.step.confirm && !progress.confirmed) throw new Error',
            html,
        )

    def test_load_reagent_first_step_requires_tubing_confirmation(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        load_plan = html.split("  load: {", 1)[1].split("  reagent: {", 1)[0]
        steps = load_plan.split("steps: [", 1)[1]
        first_step = steps.split("{title:", 1)[1].split("{title:", 1)[0]
        self.assertIn("confirm:", first_step)

    def test_dispense_map_selects_columns_and_both_384_row_bands(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        self.assertIn('id="plateMap"', html)
        self.assertIn('{section: "odd", title: "Row 1"', html)
        self.assertIn('{section: "even", title: "Row 2"', html)
        self.assertIn("function toggleBand(selection, section)", html)
        self.assertIn("function toggleColumn(selection, column)", html)
        self.assertNotIn("Partial 384-column maps are not fixture-verified", html)

    def test_protocol_panel_sits_below_the_guided_procedures(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        navigation = html.split('aria-label="Dashboard sections"', 1)[1].split("</nav>", 1)[0]
        order = [
            fragment.split('"', 1)[0]
            for fragment in navigation.split('data-panel="')[1:]
        ]
        self.assertEqual(
            order, ["load", "dispense", "reagent", "end", "protocol", "quick", "settings"]
        )
        self.assertIn('id="panel-protocol"', html)
        self.assertIn('id="protocolPalette"', html)

    def test_settings_follow_quick_and_offer_plate_geometry_defaults(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        self.assertIn('class="nav-group nav-settings"', html)
        self.assertIn('id="panel-settings"', html)
        self.assertIn('id="geometryGrid"', html)
        self.assertIn("Plate Geometry", html)
        self.assertIn("Dispense Z (steps)", html)
        self.assertIn('await api("/v1/plate-geometry"', html)
        self.assertIn('id="geometryRestore"', html)
        self.assertIn('id="geometrySave"', html)

    def test_settings_offer_automatic_maintenance_prime(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        self.assertIn('data-settings-view="maintenance"', html)
        self.assertIn('id="settings-maintenance"', html)
        self.assertIn('id="autoPrimeDue"', html)
        self.assertIn('id="autoPrimeVolume"', html)
        self.assertIn("A slow water prime runs two hours", html)
        self.assertIn("Auto prime starts without confirmation", html)
        self.assertIn('id="autoPrimeNotice"', html)
        self.assertIn("Auto prime. Wait 1 min", html)
        self.assertIn('$("autoPrimeNotice").hidden = !(', html)
        self.assertIn("const totalMinutes = Math.ceil(seconds / 60)", html)
        self.assertIn('setInterval(() => refreshAutoPrime(true), 5000)', html)
        self.assertIn('api("/v1/maintenance/auto-prime"', html)

    def test_lower_navigation_blocks_use_equal_spacers(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        self.assertIn('class="nav-group nav-primary"', html)
        self.assertIn('class="nav-group nav-protocol"', html)
        self.assertIn('class="nav-group nav-settings"', html)
        self.assertIn(
            "grid-template-rows: auto minmax(14px, 1fr) auto "
            "minmax(14px, 1fr) auto minmax(14px, 1fr)",
            html,
        )

    def test_quick_method_runs_one_operation_from_the_shared_editor(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        self.assertIn('id="panel-quick"', html)
        self.assertIn(
            'const QUICK_TYPES = ["peristaltic_dispense", "peristaltic_prime", "peristaltic_purge", "shake"];',
            html,
        )
        # Same editor as the protocol builder, so advanced settings stay in the
        # disclosure; the quick panel just drops the step-name field.
        self.assertIn("function stepEditorMarkup(step, prefix, withName)", html)
        self.assertIn('stepEditorMarkup(step, "qs", false)', html)
        self.assertIn('stepEditorMarkup(step, "ps", true)', html)
        self.assertIn("async function runQuick()", html)

    def test_protocol_builder_offers_driver_and_software_steps(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        types = html.split("const STEP_TYPES = {", 1)[1].split("};", 1)[0]
        for step_type in (
            "peristaltic_dispense",
            "peristaltic_prime",
            "peristaltic_purge",
            "shake",
            "soak",
            "wait",
            "repeat",
        ):
            self.assertIn(f"{step_type}:", types)
        # Wait and repeat must not look different to the operator.
        self.assertIn('wait: {label: "Wait", driver: false}', types)
        self.assertIn('repeat: {label: "Repeat", driver: false}', types)
        self.assertIn("function expandProtocol(steps)", html)
        self.assertIn('if (step.mode === "confirm")', html)

    def test_protocol_plate_is_inherited_by_prime_and_purge(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        resolver = html.split("function protocolPlateType(steps)", 1)[1].split(
            "function serializeProtocolSteps", 1
        )[0]
        self.assertIn('step.type === "peristaltic_dispense"', resolver)
        self.assertIn('step.type === "shake" || step.type === "soak"', resolver)
        self.assertNotIn('step.type === "peristaltic_prime"', resolver)
        self.assertIn("if (plates.size > 1) throw new Error", resolver)
        self.assertIn('return plates.values().next().value || "96_well"', resolver)
        self.assertIn('plate_type: protocolPlate || "96_well"', html)
        self.assertIn("runtimeDriverStep(entry.step, plate)", html)

    def test_shake_and_soak_offer_all_plates_independent_of_cassette(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        shake_editor = html.split(
            'step.type === "shake" || step.type === "soak"', 2
        )[2].split('} else if (step.type === "wait")', 1)[0]
        self.assertIn("PLATE_GEOMETRY_ORDER.map", shake_editor)
        self.assertNotIn("dispensePlatesForCassette", shake_editor)

    def test_protocol_steps_keep_names_and_an_advanced_disclosure(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        self.assertIn('id="protocolName"', html)
        self.assertIn('editorField("Step name"', html)
        self.assertIn('class="protocol-dispense-head"', html)
        self.assertIn('class="protocol-dispense-plate"', html)
        self.assertIn(
            'withName && step.type !== "peristaltic_dispense"', html
        )
        self.assertIn('`<details class="advanced"><summary>Advanced settings</summary>', html)
        # A saved protocol is cassette-agnostic; the fitted cassette is stamped
        # on at run time.
        self.assertIn('cassette_type: "any"', html)
        self.assertIn("cassette_type: state.cassette}", html)

    def test_volumes_are_labelled_per_well(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        for label in (
            "Volume (uL/well)",
            "Pre-dispense (uL/well)",
            "(uL/well)",
        ):
            self.assertIn(label, html)
        self.assertIn("uL/well`", html)
        self.assertNotIn("Volume per channel", html)

    def test_number_fields_open_an_on_screen_keypad(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        # The Pi has no system keyboard, so every number field must be typeable
        # through the built-in keypad.
        self.assertIn('id="keypadModal"', html)
        self.assertIn("function openKeypad(input)", html)
        self.assertIn("""input[type="number"], input[data-keypad="text"]""", html)
        self.assertIn("function keypadValid(value, input)", html)
        # Chromium reports maxLength=-1 for number inputs. Numeric keypad input
        # must therefore be unlimited instead of rejecting every entered digit.
        self.assertIn(
            'const maxLength = mode === "text" && input.maxLength > 0 ? input.maxLength : Infinity;',
            html,
        )
        self.assertNotIn("maxLength: Number(input.maxLength) || Infinity", html)
        # It respects the field's own min, max, and step.
        self.assertIn("const step = Number(input.step) || 1;", html)
        self.assertIn("return step <= 1 || Math.abs(value) % step === 0;", html)

    def test_protocol_text_fields_open_the_alphanumeric_keypad(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        self.assertIn('id="protocolName" data-keypad="text"', html)
        self.assertIn('id="${prefix}Name" data-keypad="text"', html)
        self.assertIn('id="${prefix}Message" data-keypad="text"', html)
        self.assertIn('function renderTextKeys()', html)

    def test_protocol_save_and_delete_remain_tappable_for_feedback(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        self.assertIn('$("protocolSave").disabled = running;', html)
        self.assertIn('$("protocolDelete").disabled = running;', html)
        self.assertIn('Give the protocol a name first.', html)
        self.assertIn('This protocol has not been saved yet.', html)

    def test_activity_log_is_a_settings_category(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        sidebar = html.split('<nav class="side"', 1)[1].split("</nav>", 1)[0]
        self.assertNotIn("Activity log", sidebar)
        self.assertIn('data-settings-view="geometry"', html)
        self.assertIn('data-settings-view="activity"', html)
        self.assertIn('id="settings-activity"', html)
        self.assertIn('id="log" role="log"', html)
        self.assertIn('qsa(".settings-menu button[data-settings-view]")', html)

    def test_guided_procedures_expose_editable_method_settings(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        for key in ("load", "reagent", "end"):
            self.assertIn(f'data-settings-view="{key}"', html)
            self.assertIn(f'id="settings-{key}" data-method-settings="{key}"', html)
        self.assertNotIn('id="${key}Settings"', html)
        self.assertNotIn('id="methodModal"', html)
        self.assertIn("function renderMethodSettings(key, status = null)", html)
        self.assertIn("function saveMethodSettings(key)", html)
        self.assertIn("methodVolumeId(key, index)", html)
        self.assertIn("methodSpeedId(key, index)", html)
        self.assertIn('await api("/v1/procedure-settings", {method: "POST"', html)
        self.assertIn("function procedureVolume(key, index)", html)
        self.assertIn("function procedureSpeed(key, index)", html)
        self.assertIn("flow_rate: plan.speed", html)

    def test_dashboard_compacts_usage_into_four_lifetime_quarters(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        self.assertEqual(html.count('<span class="lifetime-dot"></span>'), 4)
        self.assertIn("Math.floor(usage.percent / 25)", html)
        self.assertIn('id="cassetteSummary"', html)
        self.assertIn('id="plateCount"', html)
        self.assertNotIn('id="usageMeta"', html)
        self.assertNotIn("Four dots represent lifetime quarters", html)
        self.assertNotIn('id="usageCard"', html)

    def test_landscape_workspace_uses_the_removed_rows(self) -> None:
        html = (Path(__file__).with_name("widget.html")).read_text(encoding="utf-8")
        self.assertIn("grid-template-rows: 72px minmax(0, 1fr);", html)
        self.assertNotIn("calc(100dvh - 50px)", html)


class ProtocolLibraryTests(unittest.TestCase):
    def library(self) -> ProtocolLibrary:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "protocols.json"
        return ProtocolLibrary(self.path)

    def steps(self) -> list[dict[str, object]]:
        return [
            {
                "name": "Seed",
                "type": "peristaltic_dispense",
                "step": {"volume_ul": 10, "plate_type": "96_well", "columns": [1, 2]},
            },
            {"name": "Settle", "type": "wait", "wait": {"mode": "timed", "seconds": 30}},
            {"name": "Look", "type": "wait", "wait": {"mode": "confirm"}},
            {"name": "Again", "type": "repeat", "repeat": {"count": 3}},
        ]

    def test_saved_protocols_survive_a_reload(self) -> None:
        library = self.library()
        saved = library.save({"name": "Assay", "steps": self.steps()})
        reloaded = ProtocolLibrary(self.path).list()
        self.assertEqual([entry["name"] for entry in reloaded], ["Assay"])
        self.assertEqual(reloaded[0]["id"], saved["id"])
        self.assertEqual(len(reloaded[0]["steps"]), 4)

    def test_saving_an_existing_id_replaces_it_and_keeps_creation_time(self) -> None:
        library = self.library()
        saved = library.save({"name": "Assay", "steps": self.steps()})
        updated = library.save(
            {"id": saved["id"], "name": "Assay v2", "steps": self.steps()[:2]}
        )
        self.assertEqual(updated["created_at"], saved["created_at"])
        self.assertEqual(len(library.list()), 1)
        self.assertEqual(library.list()[0]["name"], "Assay v2")

    def test_unknown_id_and_delete_of_missing_protocol_raise(self) -> None:
        library = self.library()
        with self.assertRaises(KeyError):
            library.save({"id": "nope", "name": "x", "steps": self.steps()})
        with self.assertRaises(KeyError):
            library.delete("nope")

    def test_driver_steps_are_validated_against_the_production_models(self) -> None:
        with self.assertRaises(Exception):
            validate_library_steps(
                [{"type": "peristaltic_dispense", "step": {"volume_ul": 0}}]
            )
        with self.assertRaises(ValueError):
            validate_library_steps([{"type": "not_a_step"}])

    def test_dispense_height_is_persisted_with_xy_in_the_protocol_library(self) -> None:
        library = self.library()
        steps = self.steps()
        steps[0]["step"]["dispense_height_steps"] = 427

        saved = library.save({"name": "Adjusted height", "steps": steps})

        self.assertEqual(saved["steps"][0]["step"]["dispense_height_steps"], 427)
        self.assertIn('"dispense_height_steps": 427', self.path.read_text(encoding="utf-8"))

    def test_software_steps_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            validate_library_steps([{"type": "wait", "wait": {"mode": "timed"}}])
        with self.assertRaises(ValueError):
            validate_library_steps([{"type": "wait", "wait": {"mode": "forever"}}])
        with self.assertRaises(ValueError):
            validate_library_steps([{"type": "repeat", "repeat": {"count": 1}}])

    def test_a_repeat_needs_a_preceding_block(self) -> None:
        with self.assertRaises(ValueError):
            validate_library_steps([{"type": "repeat", "repeat": {"count": 2}}])
        with self.assertRaises(ValueError):
            validate_library_steps(
                [
                    {"type": "peristaltic_prime", "step": {"volume_ul": 100}},
                    {"type": "repeat", "repeat": {"count": 2}},
                    {"type": "repeat", "repeat": {"count": 2}},
                ]
            )


class ProcedureSettingsTests(unittest.TestCase):
    def store(self) -> ProcedureSettingsStore:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "settings.json"
        return ProcedureSettingsStore(self.path)

    def test_overrides_persist_and_reload(self) -> None:
        store = self.store()
        self.assertEqual(store.snapshot()["procedures"], {})
        store.replace(
            {
                "procedures": {
                    "end": {
                        "5ul": {
                            "volumes": [1600, 2500],
                            "speeds": ["low", "high"],
                        }
                    }
                }
            }
        )
        reloaded = ProcedureSettingsStore(self.path).snapshot()
        self.assertEqual(
            reloaded["procedures"]["end"]["5ul"],
            {"volumes": [1600, 2500], "speeds": ["low", "high"]},
        )

    def test_legacy_volume_lists_remain_compatible(self) -> None:
        store = self.store()
        saved = store.replace({"procedures": {"end": {"5ul": [1600, 2500]}}})
        self.assertEqual(saved["procedures"]["end"]["5ul"], [1600, 2500])

    def test_out_of_range_and_unknown_keys_are_rejected(self) -> None:
        for body in (
            {"procedures": {"nope": {"5ul": [1, 1]}}},
            {"procedures": {"end": {"9ul": [1, 1]}}},
            {"procedures": {"end": {"5ul": [1]}}},
            {"procedures": {"end": {"5ul": [1, 4000]}}},
            {"procedures": {"end": {"5ul": [0, 100]}}},
            {"procedures": {"end": {"5ul": {"volumes": [1, 100], "speeds": ["medium"]}}}},
            {"procedures": {"end": {"5ul": {"volumes": [1, 100], "speeds": ["medium", "turbo"]}}}},
        ):
            with self.assertRaises(ValueError):
                validate_procedure_settings(body)

    def test_empty_overrides_round_trip_to_built_in_defaults(self) -> None:
        store = self.store()
        store.replace({"procedures": {"load": {"1ul": [400, 500]}}})
        store.replace({"procedures": {}})
        self.assertEqual(ProcedureSettingsStore(self.path).snapshot()["procedures"], {})

    def test_a_stale_settings_file_does_not_block_startup(self) -> None:
        self.store()
        self.path.write_text(
            json.dumps({"cassettes": {"5ul": {"pulse_ul": 2150}}}), encoding="utf-8"
        )
        self.assertEqual(ProcedureSettingsStore(self.path).snapshot()["procedures"], {})


class AutoPrimeSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "auto_prime.json"
        self.now = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)

    def store(self) -> AutoPrimeStore:
        return AutoPrimeStore(self.path, clock=lambda: self.now)

    def test_defaults_and_schedule_persist(self) -> None:
        store = self.store()
        snapshot = store.snapshot()
        self.assertEqual(snapshot["volumes"], {"1ul": 50, "5ul": 200})
        self.assertEqual(snapshot["flow_rate"], "low")
        self.assertEqual(snapshot["interval_seconds"], 7200)
        self.assertEqual(snapshot["seconds_until_due"], 7200)

        store.replace({"volumes": {"1ul": 60, "5ul": 250}})
        reloaded = self.store().snapshot()
        self.assertEqual(reloaded["volumes"], {"1ul": 60, "5ul": 250})
        self.assertEqual(reloaded["next_due_at"], snapshot["next_due_at"])

    def test_only_completed_prime_and_dispense_reset_the_clock(self) -> None:
        store = self.store()
        result = StepResult(
            step_index=0,
            operation="peristaltic_prime",
            device_status=0,
            indication_count=0,
        )
        initial_due = store.snapshot()["next_due_at"]
        self.now += timedelta(minutes=30)
        store.record_step(uuid4(), 0, PeristalticPurge(volume_ul=100), result, CassetteType.FIVE_UL)
        self.assertEqual(store.snapshot()["next_due_at"], initial_due)

        store.record_step(uuid4(), 0, PeristalticPrime(volume_ul=100), result, CassetteType.FIVE_UL)
        self.assertEqual(store.snapshot()["seconds_until_due"], AUTO_PRIME_INTERVAL_SECONDS)
        self.now += timedelta(minutes=5)
        store.record_step(uuid4(), 0, PeristalticDispense(volume_ul=10), result, CassetteType.FIVE_UL)
        self.assertEqual(store.snapshot()["seconds_until_due"], AUTO_PRIME_INTERVAL_SECONDS)

    def test_invalid_volumes_are_rejected(self) -> None:
        for body in (
            {"volumes": {"1ul": 50}},
            {"volumes": {"1ul": 50, "5ul": 0}},
            {"volumes": {"1ul": 50, "5ul": 3001}},
            {"volumes": {"1ul": True, "5ul": 200}},
            {"volumes": {"1ul": 50, "5ul": 200, "9ul": 1}},
            {"volumes": {"1ul": 50, "5ul": 200}, "speed": "high"},
        ):
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    validate_auto_prime_settings(body)


class _AutoPrimeRunner:
    def __init__(self) -> None:
        self.active_run_id = None
        self.started_protocols = []
        self.request_ids = []
        self.statuses = {}
        self.device = SimpleNamespace(
            connected=True,
            serial_matches_expected=True,
            reconciliation_required=False,
            active_run_id=None,
            program_step_state="ready",
            modules=SimpleNamespace(
                primary_peristaltic=True,
                primary_cassette=CassetteType.FIVE_UL,
            ),
        )

    def describe_device(self) -> object:
        return self.device

    def start(
        self,
        protocol,
        *,
        operator_confirmed_idle: bool,
        request_id: str,
    ) -> StartResult:
        self.started_protocols.append(protocol)
        self.request_ids.append(request_id)
        run_id = uuid4()
        self.active_run_id = run_id
        status = RunStatus(
            run_id=run_id,
            request_id=request_id,
            protocol_name=protocol.name,
            state=RunState.QUEUED,
            total_steps=len(protocol.steps),
            completed_steps=0,
        )
        self.statuses[run_id] = status
        return StartResult(status=status, duplicate=False)

    def get(self, run_id) -> RunStatus | None:
        return self.statuses.get(run_id)


class AutoPrimeSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
        self.store = AutoPrimeStore(
            Path(self.temporary_directory.name) / "auto_prime.json",
            clock=lambda: self.now,
        )
        self.store.record_activity(at=self.now - timedelta(hours=2))
        self.runner = _AutoPrimeRunner()
        self.scheduler = AutoPrimeScheduler(
            self.runner,
            self.store,
            clock=lambda: self.now,
        )

    def test_due_prime_uses_fitted_cassette_volume_and_slow_speed(self) -> None:
        self.assertTrue(self.scheduler.check(now=self.now))
        self.assertEqual(len(self.runner.started_protocols), 1)
        step = self.runner.started_protocols[0].steps[0]
        self.assertIsInstance(step, PeristalticPrime)
        self.assertEqual(step.volume_ul, AUTO_PRIME_DEFAULT_VOLUMES_UL["5ul"])
        self.assertEqual(step.flow_rate.value, "low")
        self.assertEqual(step.cassette_type, CassetteType.FIVE_UL)
        self.assertEqual(self.scheduler.snapshot()["status"], "running")

    def test_due_prime_waits_behind_an_active_run(self) -> None:
        self.runner.active_run_id = uuid4()
        self.assertFalse(self.scheduler.check(now=self.now))
        self.assertEqual(self.runner.started_protocols, [])
        snapshot = self.scheduler.snapshot()
        self.assertEqual(snapshot["status"], "waiting")
        self.assertIn("active run", snapshot["waiting_reason"])

    def test_activity_five_minutes_before_due_moves_prime_two_hours_out(self) -> None:
        self.store.record_activity(at=self.now - timedelta(minutes=5))
        self.assertFalse(self.scheduler.check(now=self.now))
        self.assertEqual(self.runner.started_protocols, [])
        self.assertEqual(
            self.scheduler.snapshot()["seconds_until_due"],
            AUTO_PRIME_INTERVAL_SECONDS - 5 * 60,
        )

    def test_due_prime_requires_fresh_ready_device_state(self) -> None:
        self.runner.device.program_step_state = "busy"
        self.assertFalse(self.scheduler.check(now=self.now))
        self.assertEqual(self.runner.started_protocols, [])
        self.assertEqual(self.scheduler.snapshot()["status"], "waiting")


class PlateGeometrySettingsTests(unittest.TestCase):
    def store(self) -> PlateGeometryStore:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "plate_geometry.json"
        return PlateGeometryStore(self.path)

    def test_built_in_defaults_cover_every_dashboard_plate(self) -> None:
        snapshot = self.store().snapshot()
        self.assertEqual(
            set(snapshot["plates"]),
            {"96_well", "96_deep_well", "384_well", "384_deep_well"},
        )
        self.assertEqual(snapshot["plates"], snapshot["built_in"])
        self.assertEqual(snapshot["overridden_plates"], [])

    def test_overrides_are_atomic_persistent_and_restore_to_built_ins(self) -> None:
        store = self.store()
        changed = json.loads(json.dumps(BUILT_IN_PLATE_GEOMETRY))
        changed["96_well"] = {
            "x_offset_steps": 4,
            "y_offset_steps": -3,
            "dispense_height_steps": 350,
        }
        saved = store.replace({"plates": changed})
        self.assertEqual(saved["plates"]["96_well"]["x_offset_steps"], 4)
        self.assertEqual(saved["overridden_plates"], ["96_well"])
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())
        reloaded = PlateGeometryStore(self.path).snapshot()
        self.assertEqual(reloaded["plates"]["96_well"], changed["96_well"])

        restored = store.replace({"plates": BUILT_IN_PLATE_GEOMETRY})
        self.assertEqual(restored["plates"], restored["built_in"])
        self.assertEqual(restored["overridden_plates"], [])

    def test_invalid_geometry_is_rejected(self) -> None:
        valid = json.loads(json.dumps(BUILT_IN_PLATE_GEOMETRY))
        cases = []
        for plate, field, value in (
            ("96_well", "x_offset_steps", 61),
            ("96_well", "y_offset_steps", -41),
            ("96_well", "dispense_height_steps", 99),
            ("96_well", "x_offset_steps", True),
        ):
            body = json.loads(json.dumps(valid))
            body[plate][field] = value
            cases.append({"plates": body})
        cases.extend(
            [
                {"plates": {"not_a_plate": valid["96_well"]}},
                {"plates": {"96_well": {"x_offset_steps": 0}}},
            ]
        )
        for body in cases:
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    validate_plate_geometry_settings(body)

    def test_defaults_fill_only_absent_dispense_geometry(self) -> None:
        store = self.store()
        geometry = json.loads(json.dumps(BUILT_IN_PLATE_GEOMETRY))
        geometry["96_well"] = {
            "x_offset_steps": 8,
            "y_offset_steps": -5,
            "dispense_height_steps": 360,
        }
        store.replace({"plates": geometry})
        resolved = store.resolve_dispense({"volume_ul": 10, "plate_type": "96_well"})
        self.assertEqual(resolved["x_offset_steps"], 8)
        self.assertEqual(resolved["y_offset_steps"], -5)
        self.assertEqual(resolved["dispense_height_steps"], 360)

        explicit = store.resolve_dispense(
            {
                "volume_ul": 10,
                "plate_type": "96_well",
                "x_offset_steps": 0,
                "y_offset_steps": 1,
                "dispense_height_steps": 427,
            }
        )
        self.assertEqual(explicit["x_offset_steps"], 0)
        self.assertEqual(explicit["y_offset_steps"], 1)
        self.assertEqual(explicit["dispense_height_steps"], 427)

    def test_a_stale_geometry_file_does_not_block_startup(self) -> None:
        self.store()
        self.path.write_text(json.dumps({"plates": {"unknown": {}}}), encoding="utf-8")
        self.assertEqual(PlateGeometryStore(self.path).snapshot()["plates"], BUILT_IN_PLATE_GEOMETRY)


class _FakeRunner:
    state = ControllerState.RECONCILIATION_REQUIRED
    reconciliation_required = True
    active_run_id = None

    def __init__(self) -> None:
        self.reconcile_calls = 0
        self.started_protocols = []

    def reconcile_startup(self) -> ControllerState:
        self.reconcile_calls += 1
        self.state = ControllerState.IDLE
        self.reconciliation_required = False
        return self.state

    def describe_device(self) -> object:
        return SimpleNamespace(
            connected=True,
            modules=SimpleNamespace(primary_cassette=CassetteType.FIVE_UL),
        )

    def start(
        self,
        protocol,
        *,
        operator_confirmed_idle: bool,
        request_id: str,
    ) -> StartResult:
        self.started_protocols.append(protocol)
        return StartResult(
            RunStatus(
                run_id=uuid4(),
                request_id=request_id,
                protocol_name=protocol.name,
                state=RunState.QUEUED,
                total_steps=len(protocol.steps),
                completed_steps=0,
            ),
            duplicate=False,
        )


class BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.ledger_path = Path(self.temporary_directory.name) / "usage.jsonl"
        self.usage_store = CassetteUsageStore(self.ledger_path)
        self.runner = _FakeRunner()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.runner = self.runner  # type: ignore[attr-defined]
        self.server.usage_store = self.usage_store  # type: ignore[attr-defined]
        self.protocol_library = ProtocolLibrary(
            Path(self.temporary_directory.name) / "protocols.json"
        )
        self.server.protocol_library = self.protocol_library  # type: ignore[attr-defined]
        self.procedure_settings = ProcedureSettingsStore(
            Path(self.temporary_directory.name) / "settings.json"
        )
        self.server.procedure_settings = self.procedure_settings  # type: ignore[attr-defined]
        self.plate_geometry = PlateGeometryStore(
            Path(self.temporary_directory.name) / "plate_geometry.json"
        )
        self.server.plate_geometry = self.plate_geometry  # type: ignore[attr-defined]
        self.auto_prime_store = AutoPrimeStore(
            Path(self.temporary_directory.name) / "auto_prime.json"
        )
        self.auto_prime_scheduler = AutoPrimeScheduler(
            self.runner,
            self.auto_prime_store,
        )
        self.server.auto_prime_scheduler = self.auto_prime_scheduler  # type: ignore[attr-defined]
        self.shutdown_requests = []
        self.server.shutdown_callback = lambda: self.shutdown_requests.append(True)  # type: ignore[attr-defined]
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def post(self, path: str, body: object) -> tuple[int, object]:
        request = Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            return error.code, json.loads(error.read())
        with response:
            return response.status, json.loads(response.read())

    def delete(self, path: str) -> tuple[int, object]:
        request = Request(self.base_url + path, method="DELETE")
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            return error.code, json.loads(error.read())
        with response:
            return response.status, json.loads(response.read())

    def test_protocol_library_round_trips_over_http(self) -> None:
        steps = [
            {
                "name": "Seed",
                "type": "peristaltic_dispense",
                "step": {"volume_ul": 10, "plate_type": "96_well"},
            },
            {"name": "Hold", "type": "wait", "wait": {"mode": "confirm"}},
            {"name": "Again", "type": "repeat", "repeat": {"count": 2}},
        ]
        status, saved = self.post("/v1/protocols", {"name": "Assay", "steps": steps})
        self.assertEqual(status, 200)
        self.assertEqual(saved["name"], "Assay")

        with urlopen(self.base_url + "/v1/protocols", timeout=2) as response:
            listed = json.loads(response.read())
        self.assertEqual([entry["id"] for entry in listed["protocols"]], [saved["id"]])

        status, deleted = self.delete(f"/v1/protocols/{saved['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(deleted["deleted"], saved["id"])
        self.assertEqual(self.protocol_library.list(), [])

    def test_procedure_settings_round_trip_over_http(self) -> None:
        status, saved = self.post(
            "/v1/procedure-settings",
            {
                "procedures": {
                    "end": {
                        "5ul": {
                            "volumes": [1600, 2500],
                            "speeds": ["low", "high"],
                        }
                    }
                }
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["procedures"]["end"]["5ul"]["speeds"], ["low", "high"])
        with urlopen(self.base_url + "/v1/procedure-settings", timeout=2) as response:
            listed = json.loads(response.read())
        self.assertEqual(listed["procedures"]["end"]["5ul"], saved["procedures"]["end"]["5ul"])

    def test_procedure_settings_reject_out_of_range_volumes(self) -> None:
        status, payload = self.post(
            "/v1/procedure-settings", {"procedures": {"end": {"5ul": [9999, 100]}}}
        )
        self.assertEqual(status, 422)
        self.assertIn("between 1 and 3000", json.dumps(payload))

    def test_plate_geometry_round_trips_over_http(self) -> None:
        geometry = json.loads(json.dumps(BUILT_IN_PLATE_GEOMETRY))
        geometry["384_deep_well"] = {
            "x_offset_steps": -7,
            "y_offset_steps": 3,
            "dispense_height_steps": 580,
        }
        status, saved = self.post("/v1/plate-geometry", {"plates": geometry})
        self.assertEqual(status, 200)
        self.assertEqual(saved["plates"]["384_deep_well"], geometry["384_deep_well"])
        with urlopen(self.base_url + "/v1/plate-geometry", timeout=2) as response:
            listed = json.loads(response.read())
        self.assertEqual(listed["overridden_plates"], ["384_deep_well"])

    def test_plate_geometry_rejects_out_of_range_motion(self) -> None:
        geometry = json.loads(json.dumps(BUILT_IN_PLATE_GEOMETRY))
        geometry["96_well"]["dispense_height_steps"] = 1101
        status, payload = self.post("/v1/plate-geometry", {"plates": geometry})
        self.assertEqual(status, 422)
        self.assertIn("between 100 and 1100", json.dumps(payload))

    def test_auto_prime_settings_round_trip_over_http(self) -> None:
        with urlopen(self.base_url + "/v1/maintenance/auto-prime", timeout=2) as response:
            initial = json.loads(response.read())
        self.assertEqual(initial["volumes"], {"1ul": 50, "5ul": 200})
        self.assertEqual(initial["flow_rate"], "low")
        self.assertEqual(initial["status"], "scheduled")

        status, saved = self.post(
            "/v1/maintenance/auto-prime",
            {"volumes": {"1ul": 75, "5ul": 225}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["volumes"], {"1ul": 75, "5ul": 225})
        self.assertEqual(self.auto_prime_store.snapshot()["volumes"], saved["volumes"])

    def test_auto_prime_settings_endpoint_rejects_invalid_volume(self) -> None:
        status, payload = self.post(
            "/v1/maintenance/auto-prime",
            {"volumes": {"1ul": 50, "5ul": 0}},
        )
        self.assertEqual(status, 422)
        self.assertIn("between 1 and 3000", payload["detail"])

    def test_protocol_save_rejects_an_invalid_step(self) -> None:
        status, payload = self.post(
            "/v1/protocols",
            {
                "name": "Bad",
                "steps": [{"type": "wait", "wait": {"mode": "timed", "seconds": 0}}],
            },
        )
        self.assertEqual(status, 422)
        self.assertIn("seconds", json.dumps(payload))

    def test_deleting_an_unknown_protocol_is_404(self) -> None:
        status, _payload = self.delete("/v1/protocols/missing")
        self.assertEqual(status, 404)

    def test_dashboard_is_served_without_caching(self) -> None:
        with urlopen(self.base_url + "/", timeout=2) as response:
            self.assertIn("no-store", response.headers["Cache-Control"])

    def test_health_reports_reconciliation_without_touching_runner(self) -> None:
        with urlopen(self.base_url + "/v1/health", timeout=2) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["controller_state"], "reconciliation_required")
        self.assertTrue(payload["reconciliation_required"])
        self.assertEqual(self.runner.reconcile_calls, 0)

    def test_shutdown_requires_confirmation_and_an_idle_runner(self) -> None:
        status, payload = self.post("/v1/system/shutdown", {})
        self.assertEqual(status, 422)
        self.assertIn("operator_confirmed_shutdown", payload["detail"])
        self.assertEqual(self.shutdown_requests, [])

        self.runner.active_run_id = uuid4()
        status, payload = self.post(
            "/v1/system/shutdown", {"operator_confirmed_shutdown": True}
        )
        self.assertEqual(status, 409)
        self.assertIn("active", payload["detail"])
        self.assertEqual(self.shutdown_requests, [])

    def test_shutdown_accepts_the_fixed_sudo_command(self) -> None:
        status, payload = self.post(
            "/v1/system/shutdown", {"operator_confirmed_shutdown": True}
        )
        self.assertEqual(status, 202)
        self.assertEqual(payload, {"status": "shutting_down"})
        self.assertEqual(self.shutdown_requests, [True])
        self.assertEqual(
            SHUTDOWN_COMMAND,
            ("/usr/bin/sudo", "-n", "/usr/sbin/shutdown", "now"),
        )

    def test_reconcile_requires_literal_operator_confirmation(self) -> None:
        status, payload = self.post("/v1/reconcile", {})
        self.assertEqual(status, 422)
        self.assertIn("operator_confirmed_stationary", payload["detail"])
        self.assertEqual(self.runner.reconcile_calls, 0)

    def test_reconcile_returns_the_checked_controller_state(self) -> None:
        status, payload = self.post(
            "/v1/reconcile",
            {"operator_confirmed_stationary": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"controller_state": "idle"})
        self.assertEqual(self.runner.reconcile_calls, 1)

    def test_explicit_operation_routes_build_one_step_protocols(self) -> None:
        cases = (
            ("dispense", {"volume_ul": 100, "cassette_type": "5ul", "dispense_height_steps": 427}, "peristaltic_dispense"),
            ("prime", {"volume_ul": 100}, "peristaltic_prime"),
            ("purge", {"volume_ul": 100}, "peristaltic_purge"),
            ("shake", {"duration_seconds": 600}, "shake"),
            ("soak", {"duration_seconds": 600}, "soak"),
        )
        for index, (route, parameters, operation) in enumerate(cases):
            with self.subTest(route=route):
                status, payload = self.post(
                    f"/v1/operations/{route}",
                    {
                        "request_id": f"bridge-operation-{index}",
                        "operator_confirmed_idle": True,
                        **parameters,
                    },
                )
                self.assertEqual(status, 202)
                self.assertEqual(payload["total_steps"], 1)
                protocol = self.runner.started_protocols[index]
                self.assertEqual(len(protocol.steps), 1)
                self.assertEqual(protocol.steps[0].operation, operation)
                if route == "dispense":
                    self.assertEqual(protocol.steps[0].dispense_height_steps, 427)

    def test_dispense_route_applies_geometry_defaults_but_preserves_explicit_values(self) -> None:
        geometry = json.loads(json.dumps(BUILT_IN_PLATE_GEOMETRY))
        geometry["96_well"] = {
            "x_offset_steps": 6,
            "y_offset_steps": -4,
            "dispense_height_steps": 355,
        }
        self.plate_geometry.replace({"plates": geometry})
        status, _ = self.post(
            "/v1/operations/dispense",
            {
                "request_id": "geometry-defaults",
                "operator_confirmed_idle": True,
                "volume_ul": 100,
                "cassette_type": "5ul",
                "plate_type": "96_well",
            },
        )
        self.assertEqual(status, 202)
        step = self.runner.started_protocols[-1].steps[0]
        self.assertEqual(step.x_offset_steps, 6)
        self.assertEqual(step.y_offset_steps, -4)
        self.assertEqual(step.dispense_height_steps, 355)

        status, _ = self.post(
            "/v1/operations/dispense",
            {
                "request_id": "geometry-explicit",
                "operator_confirmed_idle": True,
                "volume_ul": 100,
                "cassette_type": "5ul",
                "plate_type": "96_well",
                "x_offset_steps": 0,
                "y_offset_steps": 0,
                "dispense_height_steps": 400,
            },
        )
        self.assertEqual(status, 202)
        step = self.runner.started_protocols[-1].steps[0]
        self.assertEqual(step.x_offset_steps, 0)
        self.assertEqual(step.y_offset_steps, 0)
        self.assertEqual(step.dispense_height_steps, 400)

    def test_ordered_run_and_validation_apply_geometry_defaults(self) -> None:
        geometry = json.loads(json.dumps(BUILT_IN_PLATE_GEOMETRY))
        geometry["96_deep_well"] = {
            "x_offset_steps": -2,
            "y_offset_steps": 3,
            "dispense_height_steps": 1040,
        }
        self.plate_geometry.replace({"plates": geometry})
        protocol = {
            "name": "Configured geometry",
            "steps": [
                {
                    "operation": "peristaltic_dispense",
                    "plate_type": "96_deep_well",
                    "volume_ul": 100,
                    "cassette_type": "5ul",
                }
            ],
        }
        status, validated = self.post("/v1/protocols/validate", protocol)
        self.assertEqual(status, 200)
        self.assertEqual(validated["protocol"]["steps"][0]["x_offset_steps"], -2)
        self.assertEqual(validated["protocol"]["steps"][0]["dispense_height_steps"], 1040)

        status, _ = self.post(
            "/v1/runs",
            {
                "request_id": "geometry-ordered-run",
                "operator_confirmed_idle": True,
                "protocol": protocol,
            },
        )
        self.assertEqual(status, 202)
        step = self.runner.started_protocols[-1].steps[0]
        self.assertEqual(step.x_offset_steps, -2)
        self.assertEqual(step.y_offset_steps, 3)
        self.assertEqual(step.dispense_height_steps, 1040)

    def test_operation_route_rejects_unknown_fields(self) -> None:
        status, _ = self.post(
            "/v1/operations/shake",
            {
                "request_id": "bridge-invalid",
                "operator_confirmed_idle": True,
                "duration_seconds": 5,
                "unsupported": True,
            },
        )
        self.assertEqual(status, 422)
        self.assertEqual(self.runner.started_protocols, [])

    def test_usage_endpoint_returns_both_cassette_limits(self) -> None:
        with urlopen(self.base_url + "/v1/cassette-usage", timeout=2) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["cassettes"]["1ul"]["limit_ml"], 2000)
        self.assertEqual(payload["cassettes"]["5ul"]["limit_ml"], 5000)
        self.assertEqual(payload["cassettes"]["5ul"]["used_ml"], 0)

    def test_replacement_reset_is_confirmed_and_traceable(self) -> None:
        run_id = uuid4()
        self.usage_store.record_step(
            run_id,
            0,
            PeristalticPrime(volume_ul=100, cassette_type="5ul"),
            StepResult(
                step_index=0,
                operation="peristaltic_prime",
                device_status=0,
                indication_count=0,
            ),
            CassetteType.FIVE_UL,
        )
        status, payload = self.post("/v1/cassette-usage/5ul/reset", {})
        self.assertEqual(status, 422)
        self.assertEqual(payload["detail"], "operator_confirmed_replacement must be true")

        status, payload = self.post(
            "/v1/cassette-usage/5ul/reset",
            {"operator_confirmed_replacement": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["cassettes"]["5ul"]["used_ul"], 0)
        restored = CassetteUsageStore(self.ledger_path).snapshot()
        self.assertEqual(restored["cassettes"]["5ul"]["used_ul"], 0)
        self.assertEqual(restored["recent_events"][0]["event_type"], "cassette_replaced")


class PumpedVolumeTests(unittest.TestCase):
    def test_dispense_counts_selected_wells_and_pre_dispense(self) -> None:
        step = PeristalticDispense(
            volume_ul=10,
            cassette_type="1ul",
            plate_type="96_well",
            columns=(1, 2),
            pre_dispense_volume_ul=5,
            pre_dispense_cycles=2,
        )
        self.assertEqual(pumped_volume_ul(step), 240)

    def test_384_dispense_counts_verified_row_sections(self) -> None:
        odd = PeristalticDispense(
            volume_ul=10,
            cassette_type="1ul",
            plate_type="384_well",
            row_sections="odd",
            pre_dispense_volume_ul=0,
            pre_dispense_cycles=0,
        )
        both = odd.model_copy(update={"row_sections": "all"})
        even = odd.model_copy(update={"row_sections": "even"})
        partial = odd.model_copy(update={"columns": (1, 2, 3)})
        self.assertEqual(pumped_volume_ul(odd), 1_920)
        self.assertEqual(pumped_volume_ul(both), 3_840)
        self.assertEqual(pumped_volume_ul(even), 1_920)
        self.assertEqual(pumped_volume_ul(partial), 240)

        deep_well = PeristalticDispense.model_validate(
            {**odd.model_dump(), "plate_type": "384_deep_well"}
        )
        self.assertEqual(pumped_volume_ul(deep_well), 1_920)

    def test_prime_and_purge_count_all_eight_channels(self) -> None:
        self.assertEqual(pumped_volume_ul(PeristalticPrime(volume_ul=100)), 800)
        self.assertEqual(pumped_volume_ul(PeristalticPurge(volume_ul=200)), 1_600)

    def test_non_liquid_operations_count_zero(self) -> None:
        self.assertEqual(pumped_volume_ul(Shake(duration_seconds=5)), 0)

    def test_ledger_is_idempotent_and_survives_reload(self) -> None:
        with TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "usage.jsonl"
            store = CassetteUsageStore(ledger_path)
            run_id = uuid4()
            step = PeristalticPurge(volume_ul=200, cassette_type="5ul")
            result = StepResult(
                step_index=0,
                operation=step.operation,
                device_status=0,
                indication_count=0,
            )
            store.record_step(run_id, 0, step, result, CassetteType.FIVE_UL)
            store.record_step(run_id, 0, step, result, CassetteType.FIVE_UL)
            restored = CassetteUsageStore(ledger_path).snapshot()
            self.assertEqual(restored["cassettes"]["5ul"]["used_ul"], 1_600)
            self.assertEqual(restored["cassettes"]["5ul"]["plate_count"], 0)
            self.assertEqual(len(restored["recent_events"]), 1)

    def test_plate_count_tracks_unique_dispense_runs_since_replacement(self) -> None:
        with TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "usage.jsonl"
            store = CassetteUsageStore(ledger_path)
            run_id = uuid4()
            step = PeristalticDispense(volume_ul=10, cassette_type="1ul")
            result = StepResult(
                step_index=0,
                operation=step.operation,
                device_status=0,
                indication_count=0,
            )
            store.record_step(run_id, 0, step, result, CassetteType.ONE_UL)
            store.record_step(run_id, 1, step, result, CassetteType.ONE_UL)

            restored = CassetteUsageStore(ledger_path)
            self.assertEqual(restored.snapshot()["cassettes"]["1ul"]["plate_count"], 1)

            restored.reset(CassetteType.ONE_UL)
            self.assertEqual(restored.snapshot()["cassettes"]["1ul"]["plate_count"], 0)


if __name__ == "__main__":
    unittest.main()
