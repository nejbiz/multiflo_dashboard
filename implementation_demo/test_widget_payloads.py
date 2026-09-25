"""Execute the browser run paths and validate their payloads with the driver."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from multiflo.models import Protocol


@unittest.skipUnless(shutil.which("node"), "Node.js is required for browser payload tests")
class WidgetPayloadTests(unittest.TestCase):
    def test_quick_and_protocol_payloads_match_driver_models(self):
        html = Path(__file__).with_name("widget.html").read_text(encoding="utf-8")
        functions = []
        for name in ("serializeStep", "runtimeDriverStep", "runQuick", "runProtocol"):
            match = re.search(
                rf"^(?:async )?function {name}\(.*?^}}", html, re.M | re.S
            )
            self.assertIsNotNone(match, name)
            functions.append(match.group())
        harness = r'''
const captured = [];
const state = {cassette: "5ul"};
let current;
const quickStep = () => current;
const startRun = async (kind, steps) => captured.push({kind, steps});
const toast = (message, error) => { if (error) throw Error(message); };
const log = () => {};
const renderProtocol = () => {};
const updateActions = () => {};
const $ = () => ({});
const MAX_EXPANDED_STEPS = 100;
const expandProtocol = steps => steps.map((step, index) => ({step, index}));
const protocolPlateType = () => "96_well";
const awaitRun = async () => ({state: "completed"});
const dispenseColumns = () => "all";
const is384Plate = () => false;
const axisFromOffset = (_plate, _axis, value) => value;
const dispenseHeightFromOffset = () => 336;
(async () => {
  for (const type of ["shake", "soak", "peristaltic_prime", "peristaltic_purge", "peristaltic_dispense"]) {
    current = {name: "", type, duration_seconds: 5, plate_type: "96_well",
      move_carrier_home: true, volume_ul: 50, flow_rate: "medium",
      pre_dispense_volume_ul: 0, pre_dispense_cycles: 0,
      x_offset_steps: 0, y_offset_steps: 0, z_offset_steps: 0,
      selection: {plate: "96_well", columns: new Set([1])}};
    if (type !== "soak") await runQuick();
    state.protocol = {name: "test", steps: [current]};
    await runProtocol();
  }
  process.stdout.write(JSON.stringify(captured));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        result = subprocess.run(
            [shutil.which("node")],
            input="\n".join(functions) + harness,
            text=True, capture_output=True, check=True,
        )
        captured = json.loads(result.stdout)
        self.assertEqual(len(captured), 9)
        for request in captured:
            with self.subTest(kind=request["kind"], step=request["steps"][0]):
                protocol = Protocol(name="browser payload", steps=request["steps"])
                body = request["steps"][0]
                if body["operation"] in ("shake", "soak"):
                    self.assertNotIn("cassette_type", body)
                else:
                    self.assertEqual(protocol.steps[0].cassette_type.value, "5ul")
