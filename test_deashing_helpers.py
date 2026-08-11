import sys
import types
import unittest
from itertools import permutations
from unittest.mock import patch


try:
    import serial  # noqa: F401

except ModuleNotFoundError:
    serial_module = types.ModuleType("serial")
    serial_module.EIGHTBITS = 8
    serial_module.PARITY_NONE = "N"
    serial_module.STOPBITS_ONE = 1
    serial_module.SerialException = type("SerialException", (Exception,), {})
    serial_module.Serial = None

    tools_module = types.ModuleType("serial.tools")
    list_ports_module = types.ModuleType("serial.tools.list_ports")
    list_ports_module.comports = lambda: []
    tools_module.list_ports = list_ports_module
    serial_module.tools = tools_module

    sys.modules["serial"] = serial_module
    sys.modules["serial.tools"] = tools_module
    sys.modules["serial.tools.list_ports"] = list_ports_module

from deashing_helpers import AccumetMeter, MasterflexPump, MasterflexRegloICCPump, find_devices
from greg_program_v11 import prompt_yes_no_default_yes


class FakeSerialConnection:
    def __init__(self, responses):
        self.port = "/dev/ttyUSB0"
        self.responses = iter(responses)
        self.requests = []
        self.closed = False

    def reset_input_buffer(self):
        pass

    def write(self, request):
        self.requests.append(request)

    def readline(self):
        return next(self.responses)

    def close(self):
        self.closed = True


class MasterflexPumpTests(unittest.TestCase):
    def test_probe_requires_touchscreen_status_response(self):
        connection = FakeSerialConnection([b"*\r\n", b"1, 0, 0\r\n"])

        with patch.object(
            MasterflexPump,
            "open",
            return_value=MasterflexPump(connection),
        ):
            pump = MasterflexPump.probe(connection.port)

        self.assertIsNotNone(pump)
        self.assertEqual(connection.requests, [b"1RE1\r", b"1RC\r"])
        self.assertFalse(connection.closed)

    def test_probe_rejects_generic_ack_from_another_pump(self):
        connection = FakeSerialConnection([b"*\r\n", b"#\r\n"])

        with patch.object(
            MasterflexPump,
            "open",
            return_value=MasterflexPump(connection),
        ):
            pump = MasterflexPump.probe(connection.port)

        self.assertIsNone(pump)
        self.assertTrue(connection.closed)

    def test_set_time_mode_and_run_time(self):
        connection = FakeSerialConnection([b"*\r\n", b"*\r\n"])
        pump = MasterflexPump(connection)

        pump.set_time_mode()
        pump.set_run_time_seconds(180 * 60)

        self.assertEqual(connection.requests, [b"1N\r", b"1RV0300000\r"])

    def test_run_time_supports_tenths_of_a_second(self):
        self.assertEqual(MasterflexPump.format_time_seconds(3661.2), "0101012")

    def test_run_time_rejects_values_outside_manual_range(self):
        for seconds in (0, 360000):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValueError):
                    MasterflexPump.format_time_seconds(seconds)


class PromptYesNoDefaultYesTests(unittest.TestCase):
    def test_empty_response_defaults_to_yes(self):
        with patch("builtins.input", return_value=""):
            self.assertTrue(prompt_yes_no_default_yes("Continue?"))

    def test_yes_responses_are_case_insensitive(self):
        for response in ("y", "Y", "yes", "YES", " Yes "):
            with self.subTest(response=response):
                with patch("builtins.input", return_value=response):
                    self.assertTrue(prompt_yes_no_default_yes("Continue?"))

    def test_no_responses_are_case_insensitive(self):
        for response in ("n", "N", "no", "No", "NO", " nO "):
            with self.subTest(response=response):
                with patch("builtins.input", return_value=response):
                    self.assertFalse(prompt_yes_no_default_yes("Continue?"))

    def test_invalid_response_prompts_again(self):
        with patch("builtins.input", side_effect=["maybe", "n"]):
            self.assertFalse(prompt_yes_no_default_yes("Continue?"))


class DeviceDiscoveryTests(unittest.TestCase):
    def test_main_devices_are_found_in_every_port_order(self):
        port_names = ("bath", "icc", "accumet", "touchscreen")

        def probe_accumet(port):
            if port == "accumet":
                return types.SimpleNamespace(port=port)
            return None

        def probe_touchscreen(port):
            if port == "touchscreen":
                return types.SimpleNamespace(port=port)
            return None

        for ports in permutations(port_names):
            with self.subTest(ports=ports), patch(
                "deashing_helpers.available_usb_ports", return_value=list(ports)
            ), patch("deashing_helpers.port_is_open", return_value=False), patch.object(
                AccumetMeter, "probe", side_effect=probe_accumet
            ), patch.object(
                MasterflexPump, "probe", side_effect=probe_touchscreen
            ), patch("builtins.print"):
                accumet, pump = find_devices()

            self.assertEqual(accumet.port, "accumet")
            self.assertEqual(pump.port, "touchscreen")


class MasterflexRegloICCPumpTests(unittest.TestCase):
    def test_probe_initializes_independent_channel_control(self):
        connection = FakeSerialConnection([b"2\r\n", b"*\r\n", b"*\r\n"])

        with patch.object(
            MasterflexRegloICCPump,
            "open",
            return_value=MasterflexRegloICCPump(connection),
        ):
            pump = MasterflexRegloICCPump.probe(connection.port)

        self.assertIsNotNone(pump)
        self.assertEqual(pump.address, 1)
        self.assertEqual(connection.requests, [b"0!\r", b"@1\r", b"1~1\r"])

    def test_probe_rejects_nonempty_non_icc_protocol_response(self):
        connection = FakeSerialConnection([b"#\r\n"])

        with patch.object(
            MasterflexRegloICCPump,
            "open",
            return_value=MasterflexRegloICCPump(connection),
        ):
            pump = MasterflexRegloICCPump.probe(connection.port)

        self.assertIsNone(pump)
        self.assertEqual(connection.requests, [b"0!\r"])
        self.assertTrue(connection.closed)

    def test_set_address_rejects_address_outside_manual_range(self):
        pump = MasterflexRegloICCPump(FakeSerialConnection([]))

        with self.assertRaises(ValueError):
            pump.set_address(0)

    def test_set_volume_accepts_returned_volume_response(self):
        connection = FakeSerialConnection([b"1000E+2\r\n"])
        pump = MasterflexRegloICCPump(connection)

        returned_volume = pump.set_volume_ml(100, channel=1)

        self.assertEqual(returned_volume, 100.0)
        self.assertEqual(connection.requests, [b"1v1000+2\r"])

    def test_set_run_time_uses_tenths_of_a_second(self):
        connection = FakeSerialConnection([b"*\r\n"])
        pump = MasterflexRegloICCPump(connection)

        pump.set_run_time_seconds(150, channel=1)

        self.assertEqual(connection.requests, [b"1xT00001500\r"])


if __name__ == "__main__":
    unittest.main()
