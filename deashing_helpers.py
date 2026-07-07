import datetime
import glob
import math
import os
import re
import time

import serial
from serial.tools import list_ports


DATA_ROOT = "deashing data"
RUN_FOLDER_PREFIX = "deashing"
DATA_FILENAME = "data.csv"
LOG_FILENAME = "log.txt"
VIDEO_FILENAME = "output_video.mp4"
BATH_TEMPERATURE_FILENAME = "bath_temperature.csv"
RINSE_DATA_FILENAME = "rinse_data.csv"
RINSE_BATH_TEMPERATURE_FILENAME = "rinse_bath_temperature.csv"

USB_PORT_GLOBS = ("/dev/ttyUSB*", "/dev/ttyACM*")
USB_PORT_MARKERS = ("USB", "ACM")


class AccumetMeter:
    BAUDRATE = 9600
    TIMEOUT_SECONDS = 30
    DETECTION_TIMEOUT_SECONDS = 2

    SERIAL_ENCODING = "cp437"
    COMMAND_ENCODING = 'ascii'
    EXPECTED_FIELDS = 24
    DATE_FIELD = 5
    TIME_FIELD = 6
    CONDUCTIVITY_FIELD = 8
    TEMPERATURE_FIELD = 12

    PROMPT = "> "
    SYSTEM_COMMAND = "SYSTEM\r"
    SET_CSV_COMMAND = "SETCSV\r"

    def __init__(self, connection):
        self.connection = connection
        self.port = connection.port

    @classmethod
    def open(cls, port, timeout=None):
        if timeout is None:
            timeout = cls.TIMEOUT_SECONDS

        connection = open_serial_port(
            port,
            baudrate=cls.BAUDRATE,
            timeout=timeout,
        )
        return cls(connection)

    @classmethod
    def probe(cls, port):
        meter = None
        try:
            meter = cls.open(port, timeout=cls.DETECTION_TIMEOUT_SECONDS)
            response = meter.command(cls.SYSTEM_COMMAND)
            if "accumet" in response.lower() or "ab330" in response.lower():
                return meter
            meter.close()
        except (OSError, RuntimeError, serial.SerialException, UnicodeDecodeError):
            if meter is not None:
                meter.close()

        return None

    def command(self, command):
        self.connection.reset_input_buffer()
        self.connection.write(command.encode(self.COMMAND_ENCODING))
        return self.read_until_prompt()

    def read_until_prompt(self):
        chunks = []
        deadline = time.monotonic() + self.connection.timeout

        while time.monotonic() < deadline:
            chunk = self.connection.read(1)
            if not chunk:
                continue

            chunks.append(chunk)
            if b"".join(chunks).endswith(self.PROMPT.encode(self.COMMAND_ENCODING)):
                break

        return b"".join(chunks).decode(self.SERIAL_ENCODING, errors="replace")

    def set_csv_output(self):
        self.command(self.SET_CSV_COMMAND)

    def reset_input_buffer(self):
        self.connection.reset_input_buffer()

    def read_measurement(self):
        fields = self.connection.readline().decode(self.SERIAL_ENCODING).split(",")
        if len(fields) != self.EXPECTED_FIELDS:
            return None

        try:
            return {
                "date": fields[self.DATE_FIELD],
                "time": fields[self.TIME_FIELD],
                "conductivity": float(fields[self.CONDUCTIVITY_FIELD]),
                "temperature": float(fields[self.TEMPERATURE_FIELD]),
            }
        except ValueError:
            return None

    def close(self):
        self.connection.close()


class MasterflexPump:
    """Serial driver for a Masterflex touchscreen pump using the RS-232 protocol.

    Commands are sent as: address + command + optional parameter + carriage return.
    The default address is 1, and valid pump addresses are 1 through 8.
    """

    BAUDRATE = 115200
    TIMEOUT_SECONDS = 5
    DETECTION_TIMEOUT_SECONDS = 2

    DEFAULT_ADDRESS = 1
    MIN_ADDRESS = 1
    MAX_ADDRESS = 8

    ACK = b"*"
    NOT_IN_REMOTE_MODE = b"~"
    INVALID_COMMAND = b"#"
    COMMAND_ENCODING = 'ascii'
    RESPONSE_ENCODING = 'ascii'
    TERMINATOR = "\r"

    CLOCKWISE = "J"
    COUNTERCLOCKWISE = "K"
    COMMAND_DELAY_SECONDS = 1

    def __init__(self, connection, address=DEFAULT_ADDRESS):
        self.connection = connection
        self.port = connection.port
        self.address = self.validate_address(address)

    @staticmethod
    def validate_address(address):
        address = int(address)
        if not MasterflexPump.MIN_ADDRESS <= address <= MasterflexPump.MAX_ADDRESS:
            raise ValueError("Masterflex pump address must be between 1 and 8")
        return address

    @classmethod
    def open(cls, port, timeout=None, address=DEFAULT_ADDRESS):
        connection = open_serial_port(
            port,
            baudrate=cls.BAUDRATE,
            timeout=cls.TIMEOUT_SECONDS if timeout is None else timeout,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
        )
        return cls(connection, address=address)

    @classmethod
    def probe(cls, port):
        pump = None
        try:
            pump = cls.open(port, timeout=cls.DETECTION_TIMEOUT_SECONDS)
            response = pump.enable_remote()
            if response in (cls.ACK, cls.NOT_IN_REMOTE_MODE):
                return pump
            pump.close()
        except (OSError, RuntimeError, serial.SerialException, UnicodeDecodeError):
            if pump is not None:
                pump.close()

        return None

    @staticmethod
    def format_rpm(rpm):
        scaled_rpm = round(float(rpm) * 100)
        if scaled_rpm < 0:
            raise ValueError("RPM must be greater than or equal to zero")
        return f"{scaled_rpm:05d}"

    @staticmethod
    def format_percent(percent):
        scaled_percent = round(float(percent) * 10)
        if scaled_percent < 0:
            raise ValueError("Percent speed must be greater than or equal to zero")
        return f"{scaled_percent:05d}"

    def build_command(self, command, parameter=None, address=None):
        if address is None:
            address = self.address
        address = self.validate_address(address)
        parameter_text = "" if parameter is None else str(parameter)
        return f"{address}{command}{parameter_text}{self.TERMINATOR}"

    def command(self, command, parameter=None, address=None):
        request = self.build_command(command, parameter=parameter, address=address)
        self.connection.reset_input_buffer()
        self.connection.write(request.encode(self.COMMAND_ENCODING))
        return self.connection.readline().strip()

    def query(self, command, address=None):
        return self.command(command, address=address).decode(self.RESPONSE_ENCODING)

    def require_ack(self, response):
        if response != self.ACK:
            raise RuntimeError(f"Masterflex command failed: {response!r}")
        return response

    def set_address(self, new_address):
        new_address = self.validate_address(new_address)
        self.connection.reset_input_buffer()
        self.connection.write(f"@{new_address}{self.TERMINATOR}".encode(self.COMMAND_ENCODING))
        response = self.connection.readline().strip()
        self.require_ack(response)
        self.address = new_address
        return response

    def enable_remote(self):
        return self.command("RE", "1")

    def disable_remote(self):
        return self.command("RE", "0")

    def enable_serial_remote_mode(self):
        return self.enable_remote()

    def disable_serial_remote_mode(self):
        return self.disable_remote()

    def set_speed_rpm(self, rpm):
        return self.command("R", self.format_rpm(rpm))

    def read_speed_rpm(self):
        return float(self.query("R"))

    def set_speed_percent(self, percent):
        return self.command("S", self.format_percent(percent))

    def read_speed_percent(self):
        return float(self.query("S"))

    def set_speed(self, rpm):
        return self.set_speed_rpm(rpm)

    def start(self):
        return self.command("H")

    def stop(self):
        return self.command("I")

    def set_clockwise(self):
        return self.command(self.CLOCKWISE)

    def set_counterclockwise(self):
        return self.command(self.COUNTERCLOCKWISE)

    def read_status(self):
        response = self.query("RC")
        address, running, counterclockwise = (int(value.strip()) for value in response.split(","))
        return {
            "address": address,
            "running": bool(running),
            "counterclockwise": bool(counterclockwise),
        }

    def close(self):
        self.connection.close()


class FisherIsotempBath:
    BAUDRATE = 9600
    TIMEOUT_SECONDS = 5
    DETECTION_TIMEOUT_SECONDS = 2

    COMMAND_ENCODING = 'ascii'
    RESPONSE_ENCODING = 'ascii'
    TERMINATOR = "\r"
    OK_RESPONSE = "OK"
    ERROR_PREFIX = "?"

    READ_TEMPERATURE_COMMAND = "RT"
    READ_EXTERNAL_TEMPERATURE_COMMAND = "RT2"
    READ_DISPLAYED_SETPOINT_COMMAND = "RS"
    SET_DISPLAYED_SETPOINT_COMMAND = "SS"
    READ_SETPOINT_COMMAND = "RS{slot}"
    SET_SETPOINT_COMMAND = "SS{slot}"
    READ_HIGH_TEMPERATURE_FAULT_COMMAND = "RHTF"
    SET_HIGH_TEMPERATURE_FAULT_COMMAND = "SHTF"
    READ_HIGH_TEMPERATURE_WARN_COMMAND = "RHTW"
    SET_HIGH_TEMPERATURE_WARN_COMMAND = "SHTW"
    READ_LOW_TEMPERATURE_FAULT_COMMAND = "RLTF"
    SET_LOW_TEMPERATURE_FAULT_COMMAND = "SLTF"
    READ_LOW_TEMPERATURE_WARN_COMMAND = "RLTW"
    SET_LOW_TEMPERATURE_WARN_COMMAND = "SLTW"
    READ_TEMPERATURE_PRECISION_COMMAND = "RTP"
    SET_TEMPERATURE_RESOLUTION_COMMAND = "STR"
    READ_TEMPERATURE_UNITS_COMMAND = "RTU"
    SET_TEMPERATURE_UNITS_COMMAND = "STU"
    READ_UNIT_ON_COMMAND = "RO"
    SET_UNIT_ON_COMMAND = "SO"
    READ_EXTERNAL_PROBE_COMMAND = "RE"
    SET_EXTERNAL_PROBE_COMMAND = "SE"
    READ_AUTO_RESTART_COMMAND = "RAR"
    SET_AUTO_RESTART_COMMAND = "SAR"
    READ_ENERGY_SAVING_COMMAND = "REN"
    SET_ENERGY_SAVING_COMMAND = "SEN"
    READ_TIME_COMMAND = "RCK"
    READ_DATE_COMMAND = "RDT"
    READ_DATE_FORMAT_COMMAND = "RDF"
    READ_RAMP_STATUS_COMMAND = "RRS"
    SET_PUMP_SPEED_COMMAND = "SPS"
    SET_RAMP_NUMBER_COMMAND = "SRN"
    READ_FIRMWARE_VERSION_COMMAND = "RVER"
    READ_FIRMWARE_CHECKSUM_COMMAND = "RSUM"
    READ_UNIT_FAULT_STATUS_COMMAND = "RUFS"

    def __init__(self, connection):
        self.connection = connection
        self.port = connection.port

    @classmethod
    def open(cls, port, baudrate=None, timeout=None):
        if baudrate is None:
            baudrate = cls.BAUDRATE
        if timeout is None:
            timeout = cls.TIMEOUT_SECONDS

        connection = open_serial_port(
            port,
            baudrate=baudrate,
            timeout=timeout,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
        )
        return cls(connection)

    @classmethod
    def probe(cls, port):
        bath = None
        try:
            bath = cls.open(port, timeout=cls.DETECTION_TIMEOUT_SECONDS)
            bath.read_temperature()
            return bath
        except (OSError, RuntimeError, serial.SerialException, UnicodeDecodeError, ValueError):
            if bath is not None:
                bath.close()

        return None

    def command(self, command):
        self.connection.reset_input_buffer()
        self.connection.write(f"{command}{self.TERMINATOR}".encode(self.COMMAND_ENCODING))
        response = self.connection.readline().decode(self.RESPONSE_ENCODING).strip()
        if response.startswith(self.ERROR_PREFIX):
            raise RuntimeError(f"Isotemp error response: {response}")
        return response

    def set_command(self, command, value):
        response = self.command(f"{command} {value}")
        if response != self.OK_RESPONSE:
            raise RuntimeError(f"Unexpected Isotemp response: {response}")
        return response

    @staticmethod
    def parse_value_units(response):
        parts = response.split(maxsplit=1)
        if not parts:
            raise ValueError("Empty Isotemp response")

        value_text = parts[0]
        units = parts[1] if len(parts) > 1 else ""
        if value_text[-1:].isalpha() and not units:
            return float(value_text[:-1]), value_text[-1]

        return float(value_text), units

    @staticmethod
    def parse_int_list(response):
        return [int(value.strip()) for value in response.split(",")]

    def read_temperature(self):
        value, _units = self.parse_value_units(self.command(self.READ_TEMPERATURE_COMMAND))
        return value

    def read_external_temperature(self):
        value, _units = self.parse_value_units(self.command(self.READ_EXTERNAL_TEMPERATURE_COMMAND))
        return value

    def read_displayed_setpoint(self):
        value, _units = self.parse_value_units(self.command(self.READ_DISPLAYED_SETPOINT_COMMAND))
        return value

    def set_displayed_setpoint(self, temperature):
        return self.set_command(self.SET_DISPLAYED_SETPOINT_COMMAND, temperature)

    def read_setpoint(self, slot):
        value, _units = self.parse_value_units(
            self.command(self.READ_SETPOINT_COMMAND.format(slot=slot))
        )
        return value

    def set_setpoint(self, slot, temperature):
        return self.set_command(self.SET_SETPOINT_COMMAND.format(slot=slot), temperature)

    def read_high_temperature_fault(self):
        value, _units = self.parse_value_units(self.command(self.READ_HIGH_TEMPERATURE_FAULT_COMMAND))
        return value

    def set_high_temperature_fault(self, temperature):
        return self.set_command(self.SET_HIGH_TEMPERATURE_FAULT_COMMAND, temperature)

    def read_high_temperature_warning(self):
        value, _units = self.parse_value_units(self.command(self.READ_HIGH_TEMPERATURE_WARN_COMMAND))
        return value

    def set_high_temperature_warning(self, temperature):
        return self.set_command(self.SET_HIGH_TEMPERATURE_WARN_COMMAND, temperature)

    def read_low_temperature_fault(self):
        value, _units = self.parse_value_units(self.command(self.READ_LOW_TEMPERATURE_FAULT_COMMAND))
        return value

    def set_low_temperature_fault(self, temperature):
        return self.set_command(self.SET_LOW_TEMPERATURE_FAULT_COMMAND, temperature)

    def read_low_temperature_warning(self):
        value, _units = self.parse_value_units(self.command(self.READ_LOW_TEMPERATURE_WARN_COMMAND))
        return value

    def set_low_temperature_warning(self, temperature):
        return self.set_command(self.SET_LOW_TEMPERATURE_WARN_COMMAND, temperature)

    def read_temperature_precision(self):
        return float(self.command(self.READ_TEMPERATURE_PRECISION_COMMAND))

    def set_temperature_resolution(self, resolution):
        return self.set_command(self.SET_TEMPERATURE_RESOLUTION_COMMAND, resolution)

    def read_temperature_units(self):
        return self.command(self.READ_TEMPERATURE_UNITS_COMMAND)

    def set_temperature_units(self, units):
        return self.set_command(self.SET_TEMPERATURE_UNITS_COMMAND, units)

    def read_unit_on(self):
        return self.command(self.READ_UNIT_ON_COMMAND) == "1"

    def set_unit_on(self, enabled):
        return self.set_command(self.SET_UNIT_ON_COMMAND, int(bool(enabled)))

    def set_unit_off(self):
        return self.set_unit_on(False)

    def read_external_probe_enabled(self):
        return self.command(self.READ_EXTERNAL_PROBE_COMMAND) == "1"

    def set_external_probe_enabled(self, enabled):
        return self.set_command(self.SET_EXTERNAL_PROBE_COMMAND, int(bool(enabled)))

    def read_auto_restart_enabled(self):
        return self.command(self.READ_AUTO_RESTART_COMMAND) == "1"

    def set_auto_restart_enabled(self, enabled):
        return self.set_command(self.SET_AUTO_RESTART_COMMAND, int(bool(enabled)))

    def read_energy_saving_mode(self):
        return self.command(self.READ_ENERGY_SAVING_COMMAND)

    def set_energy_saving_mode(self, mode):
        return self.set_command(self.SET_ENERGY_SAVING_COMMAND, mode)

    def read_time(self):
        return self.command(self.READ_TIME_COMMAND)

    def read_date(self):
        return self.command(self.READ_DATE_COMMAND)

    def read_date_format(self):
        return self.command(self.READ_DATE_FORMAT_COMMAND)

    def read_ramp_status(self):
        return self.command(self.READ_RAMP_STATUS_COMMAND)

    def set_pump_speed(self, speed):
        return self.set_command(self.SET_PUMP_SPEED_COMMAND, speed)

    def set_ramp_number(self, ramp_number):
        return self.set_command(self.SET_RAMP_NUMBER_COMMAND, ramp_number)

    def read_firmware_version(self):
        return self.command(self.READ_FIRMWARE_VERSION_COMMAND)

    def read_firmware_checksum(self):
        return self.command(self.READ_FIRMWARE_CHECKSUM_COMMAND)

    def read_unit_fault_status(self):
        return self.parse_int_list(self.command(self.READ_UNIT_FAULT_STATUS_COMMAND))

    def close(self):
        self.connection.close()


class MasterflexRegloICCPump:
    BAUDRATE = 9600
    TIMEOUT_SECONDS = 5
    DETECTION_TIMEOUT_SECONDS = 2

    COMMAND_ENCODING = 'ascii'
    RESPONSE_ENCODING = 'ascii'
    TERMINATOR = "\r"
    PARAMETER_DELIMITER = "|"

    DEFAULT_ADDRESS = 0
    STATUS_OK = "*"
    STATUS_ERROR = "#"
    STATUS_NEGATIVE = "-"
    STATUS_POSITIVE = "+"

    CLOCKWISE = "J"
    COUNTERCLOCKWISE = "K"

    MODE_RPM = "L"
    MODE_FLOW_RATE = "M"
    MODE_VOLUME_AT_RATE = "O"
    MODE_VOLUME_OVER_TIME = "G"
    MODE_VOLUME_PAUSE = "Q"
    MODE_TIME = "N"
    MODE_TIME_PAUSE = "P"

    LANGUAGE_ENGLISH = "0"
    LANGUAGE_FRENCH = "1"
    LANGUAGE_SPANISH = "2"
    LANGUAGE_GERMAN = "3"

    def __init__(self, connection, address=DEFAULT_ADDRESS):
        self.connection = connection
        self.port = connection.port
        self.address = address

    @classmethod
    def open(cls, port, timeout=None, address=DEFAULT_ADDRESS):
        if timeout is None:
            timeout = cls.TIMEOUT_SECONDS

        connection = open_serial_port(
            port,
            baudrate=cls.BAUDRATE,
            timeout=timeout,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
        )
        return cls(connection, address=address)

    @classmethod
    def probe(cls, port):
        pump = None
        try:
            pump = cls.open(port, timeout=cls.DETECTION_TIMEOUT_SECONDS)
            if pump.get_protocol_version():
                return pump
            pump.close()
        except (OSError, RuntimeError, serial.SerialException, UnicodeDecodeError, ValueError):
            if pump is not None:
                pump.close()

        return None

    @staticmethod
    def format_discrete(value, width):
        return f"{int(value):0{width}d}"

    @staticmethod
    def format_time_seconds(seconds):
        tenths = int(round(float(seconds) * 10))
        return MasterflexRegloICCPump.format_discrete(tenths, 8)

    @staticmethod
    def parse_time_seconds(response):
        return int(response) / 10

    @staticmethod
    def format_rpm(rpm):
        return MasterflexRegloICCPump.format_discrete(round(float(rpm) * 100), 6)

    @staticmethod
    def parse_volume_response(response):
        if "E" not in response:
            return float(response.split()[0])

        mantissa = int(response[:4])
        exponent = int(response[5:])
        return (mantissa / 1000) * (10 ** exponent)

    @staticmethod
    def format_volume_parameter(volume_ml):
        if volume_ml <= 0:
            return "0000+0"

        exponent = math.floor(math.log10(abs(volume_ml)))
        mantissa = round((volume_ml / (10 ** exponent)) * 1000)
        if mantissa >= 10000:
            mantissa = 1000
            exponent += 1

        sign = "+" if exponent >= 0 else "-"
        return f"{mantissa:04d}{sign}{abs(exponent)}"

    def build_command(self, command, parameters=None, address=None):
        if address is None:
            address = self.address
        if parameters is None:
            parameters = []
        elif not isinstance(parameters, (list, tuple)):
            parameters = [parameters]

        parameter_text = self.PARAMETER_DELIMITER.join(str(parameter) for parameter in parameters)
        return f"{address}{command}{parameter_text}{self.TERMINATOR}"

    def command(self, command, parameters=None, address=None):
        request = self.build_command(command, parameters=parameters, address=address)
        self.connection.reset_input_buffer()
        self.connection.write(request.encode(self.COMMAND_ENCODING))
        return self.connection.readline().decode(self.RESPONSE_ENCODING).strip()

    def require_ok(self, response):
        if response != self.STATUS_OK:
            raise RuntimeError(f"Reglo ICC command failed: {response}")
        return response

    def require_positive(self, response):
        if response != self.STATUS_POSITIVE:
            raise RuntimeError(f"Reglo ICC command failed: {response}")
        return response

    def set_address(self, address):
        self.connection.reset_input_buffer()
        self.connection.write(f"@{address}{self.TERMINATOR}".encode(self.COMMAND_ENCODING))
        return self.require_ok(self.connection.readline().decode(self.RESPONSE_ENCODING).strip())

    def get_channel_addressing_enabled(self):
        return self.command("~") == "1"

    def set_channel_addressing_enabled(self, enabled):
        return self.require_ok(self.command("~", int(bool(enabled))))

    def get_event_messages_enabled(self):
        return self.command("xE") == "1"

    def set_event_messages_enabled(self, enabled):
        return self.require_ok(self.command("xE", int(bool(enabled))))

    def get_protocol_version(self):
        return self.command("!")

    def start(self, channel=None):
        return self.require_ok(self.command("H", address=self.address_for_channel(channel)))

    def stop(self, channel=None):
        return self.require_ok(self.command("I", address=self.address_for_channel(channel)))

    def pause(self, channel=None):
        return self.require_ok(self.command("xI", address=self.address_for_channel(channel)))

    def get_cannot_run_cause(self, channel=None):
        response = self.command("xe", address=self.address_for_channel(channel))
        return response.split()

    def get_direction(self, channel=None):
        return self.command("xD", address=self.address_for_channel(channel))

    def set_direction(self, direction, channel=None):
        if direction not in (self.CLOCKWISE, self.COUNTERCLOCKWISE):
            raise ValueError("Direction must be 'J' for clockwise or 'K' for counter-clockwise")
        return self.require_ok(self.command(direction, address=self.address_for_channel(channel)))

    def set_clockwise(self, channel=None):
        return self.set_direction(self.CLOCKWISE, channel=channel)

    def set_counterclockwise(self, channel=None):
        return self.set_direction(self.COUNTERCLOCKWISE, channel=channel)

    def get_mode(self, channel=None):
        return self.command("xM", address=self.address_for_channel(channel))

    def set_mode(self, mode, channel=None):
        return self.require_ok(self.command(mode, address=self.address_for_channel(channel)))

    def set_rpm_mode(self, channel=None):
        return self.set_mode(self.MODE_RPM, channel=channel)

    def set_flow_rate_mode(self, channel=None):
        return self.set_mode(self.MODE_FLOW_RATE, channel=channel)

    def set_volume_at_rate_mode(self, channel=None):
        return self.set_mode(self.MODE_VOLUME_AT_RATE, channel=channel)

    def set_volume_over_time_mode(self, channel=None):
        return self.set_mode(self.MODE_VOLUME_OVER_TIME, channel=channel)

    def set_volume_pause_mode(self, channel=None):
        return self.set_mode(self.MODE_VOLUME_PAUSE, channel=channel)

    def set_time_mode(self, channel=None):
        return self.set_mode(self.MODE_TIME, channel=channel)

    def set_time_pause_mode(self, channel=None):
        return self.set_mode(self.MODE_TIME_PAUSE, channel=channel)

    def get_speed_rpm(self, channel=None):
        return float(self.command("S", address=self.address_for_channel(channel)))

    def set_speed_rpm(self, rpm, channel=None):
        return self.require_ok(
            self.command("S", self.format_rpm(rpm), address=self.address_for_channel(channel))
        )

    def get_flow_rate_ml_min(self, channel=None):
        return self.parse_volume_response(self.command("f", address=self.address_for_channel(channel)))

    def set_flow_rate_ml_min(self, flow_rate, channel=None):
        return self.command("f", self.format_volume_parameter(flow_rate), address=self.address_for_channel(channel))

    def get_volume_ml(self, channel=None):
        return self.parse_volume_response(self.command("v", address=self.address_for_channel(channel)))

    def set_volume_ml(self, volume, channel=None):
        return self.command("v", self.format_volume_parameter(volume), address=self.address_for_channel(channel))

    def get_run_time_seconds(self, channel=None):
        return self.parse_time_seconds(self.command("xT", address=self.address_for_channel(channel)))

    def get_run_time_raw(self, channel=None):
        return int(self.command("xT", address=self.address_for_channel(channel)))

    def set_run_time_seconds(self, seconds, channel=None):
        return self.require_ok(
            self.command("xT", self.format_time_seconds(seconds), address=self.address_for_channel(channel))
        )

    def get_pause_time_seconds(self, channel=None):
        return self.parse_time_seconds(self.command("xP", address=self.address_for_channel(channel)))

    def get_pause_time_raw(self, channel=None):
        return int(self.command("xP", address=self.address_for_channel(channel)))

    def set_pause_time_seconds(self, seconds, channel=None):
        return self.require_ok(
            self.command("xP", self.format_time_seconds(seconds), address=self.address_for_channel(channel))
        )

    def get_cycle_count(self, channel=None):
        return int(self.command('"', address=self.address_for_channel(channel)))

    def set_cycle_count(self, count, channel=None):
        return self.require_ok(
            self.command('"', self.format_discrete(count, 4), address=self.address_for_channel(channel))
        )

    def get_max_flow_rate_ml_min(self, channel=None):
        return float(self.command("?", address=self.address_for_channel(channel)).split()[0])

    def get_calibrated_max_flow_rate_ml_min(self, channel=None):
        return float(self.command("!", address=self.address_for_channel(channel)).split()[0])

    def get_dispense_time_for_volume_and_flow_seconds(self, volume_ml, flow_rate_ml_min, channel=None):
        response = self.command(
            "xv",
            [self.format_volume_parameter(volume_ml), self.format_volume_parameter(flow_rate_ml_min)],
            address=self.address_for_channel(channel),
        )
        return self.parse_time_seconds(response)

    def get_dispense_time_for_volume_and_rpm_seconds(self, volume_ml, rpm, channel=None):
        response = self.command(
            "xw",
            [self.format_volume_parameter(volume_ml), self.format_rpm(rpm)],
            address=self.address_for_channel(channel),
        )
        return self.parse_time_seconds(response)

    def get_tubing_inner_diameter_mm(self, channel=None):
        response = self.command("+", address=self.address_for_channel(channel))
        return float(response.split()[0])

    def set_tubing_inner_diameter_mm(self, diameter, channel=None):
        return self.require_ok(
            self.command("+", self.format_discrete(round(float(diameter) * 100), 4),
                         address=self.address_for_channel(channel))
        )

    def get_backsteps(self, channel=None):
        return int(self.command("%", address=self.address_for_channel(channel)))

    def set_backsteps(self, backsteps, channel=None):
        return self.require_ok(
            self.command("%", self.format_discrete(backsteps, 4), address=self.address_for_channel(channel))
        )

    def reset_defaults(self, channel=None):
        return self.require_ok(self.command("0", address=self.address_for_channel(channel)))

    def get_calibration_direction(self, channel=None):
        return self.command("xR", address=self.address_for_channel(channel))

    def set_calibration_direction(self, direction, channel=None):
        return self.require_ok(self.command("xR", direction, address=self.address_for_channel(channel)))

    def get_calibration_target_volume_ml(self, channel=None):
        return self.parse_volume_response(self.command("xU", address=self.address_for_channel(channel)))

    def set_calibration_target_volume_ml(self, volume, channel=None):
        return self.command("xU", self.format_volume_parameter(volume), address=self.address_for_channel(channel))

    def set_calibration_measured_volume_ml(self, volume, channel=None):
        return self.command("xV", self.format_volume_parameter(volume), address=self.address_for_channel(channel))

    def get_calibration_time_seconds(self, channel=None):
        return self.parse_time_seconds(self.command("xW", address=self.address_for_channel(channel)))

    def get_calibration_time_raw(self, channel=None):
        return int(self.command("xW", address=self.address_for_channel(channel)))

    def set_calibration_time_seconds(self, seconds, channel=None):
        return self.require_ok(
            self.command("xW", self.format_time_seconds(seconds), address=self.address_for_channel(channel))
        )

    def get_time_since_last_calibration_seconds(self, channel=None):
        return self.parse_time_seconds(self.command("xX", address=self.address_for_channel(channel)))

    def get_time_since_last_calibration_raw(self, channel=None):
        return int(self.command("xX", address=self.address_for_channel(channel)))

    def start_calibration(self, channel=None):
        return self.require_ok(self.command("xY", address=self.address_for_channel(channel)))

    def cancel_calibration(self, channel=None):
        return self.require_ok(self.command("xZ", address=self.address_for_channel(channel)))

    def get_firmware_version(self):
        return self.command("(", address=0)

    def set_factory_roller_step_volume(self, roller_count, tubing_index, roller_step_volume_ml):
        return self.require_ok(
            self.command(
                "xt",
                [
                    int(roller_count),
                    int(tubing_index),
                    self.format_volume_parameter(roller_step_volume_ml),
                ],
                address=0,
            )
        )

    def save_roller_step_settings(self):
        return self.require_ok(self.command("xs", address=0))

    def reset_roller_step_volume_table(self):
        return self.require_ok(self.command("xu", address=0))

    def set_display_name(self, name):
        return self.require_ok(self.command("xN", name, address=0))

    def get_serial_number(self):
        return self.command("xS", address=0)

    def set_serial_number(self, serial_number):
        return self.require_ok(self.command("xS", serial_number, address=0))

    def get_language(self):
        return self.command("xL", address=0)

    def set_language(self, language):
        return self.require_ok(self.command("xL", language, address=0))

    def get_channel_count(self):
        return int(self.command("xA", address=0))

    def set_channel_count(self, count):
        return self.require_ok(self.command("xA", self.format_discrete(count, 4), address=0))

    def get_roller_count(self, channel=None):
        return int(self.command("xB", address=self.address_for_channel(channel)))

    def set_roller_count(self, count, channel=None):
        return self.require_ok(
            self.command("xB", self.format_discrete(count, 4), address=self.address_for_channel(channel))
        )

    def get_total_revolutions(self, channel=None):
        return int(self.command("xC", address=self.address_for_channel(channel)))

    def get_channel_total_volume_ml(self, channel=None):
        return int(self.command("xG", address=self.address_for_channel(channel)))

    def get_channel_total_time_seconds(self, channel=None):
        return int(self.command("xJ", address=self.address_for_channel(channel)))

    def enable_user_interface_control(self):
        return self.require_ok(self.command("A", address=0))

    def disable_user_interface_control(self):
        return self.require_ok(self.command("B", address=0))

    def display_numbers(self, text):
        return self.require_ok(self.command("D", str(text)[:16], address=0))

    def display_letters(self, text):
        return self.require_ok(self.command("DA", str(text)[:16], address=0))

    def get_running(self):
        return self.command("E", address=0) == self.STATUS_POSITIVE

    def get_pump_info(self):
        return self.command("#", address=0)

    def get_pump_head_code(self):
        return self.command(")", address=0)

    def set_pump_head_code(self, code):
        return self.require_ok(self.command(")", self.format_discrete(code, 4), address=0))

    def get_pump_time_tenths(self):
        return int(self.command("V", address=0))

    def set_pump_time_tenths(self, tenths):
        return self.require_ok(self.command("V", self.format_discrete(tenths, 4), address=0))

    def set_pump_time_minutes(self, minutes):
        return self.require_ok(self.command("VM", self.format_discrete(minutes, 3), address=0))

    def set_pump_time_hours(self, hours):
        return self.require_ok(self.command("VH", self.format_discrete(hours, 3), address=0))

    def get_low_order_roller_steps(self):
        return int(self.command("U", address=0))

    def set_low_order_roller_steps(self, steps):
        return self.require_ok(self.command("U", self.format_discrete(steps, 5), address=0))

    def get_high_order_roller_steps(self):
        return int(self.command("u", address=0))

    def set_high_order_roller_steps(self, steps):
        return self.require_ok(self.command("u", self.format_discrete(steps, 5), address=0))

    def get_roller_step_volume_ml(self, channel=None):
        return self.parse_volume_response(self.command("r", address=self.address_for_channel(channel)))

    def set_roller_step_volume_ml(self, volume, channel=None):
        return self.require_ok(
            self.command("r", self.format_volume_parameter(volume), address=self.address_for_channel(channel))
        )

    def reset_calibration_to_default_roller_step_volume(self, channel=None):
        return self.require_ok(self.command("000000", address=self.address_for_channel(channel)))

    def get_pause_time_tenths(self):
        return int(self.command("T", address=0))

    def set_pause_time_tenths(self, tenths):
        return self.require_ok(self.command("T", self.format_discrete(tenths, 4), address=0))

    def set_pause_time_minutes(self, minutes):
        return self.require_ok(self.command("TM", self.format_discrete(minutes, 3), address=0))

    def set_pause_time_hours(self, hours):
        return self.require_ok(self.command("TH", self.format_discrete(hours, 3), address=0))

    def get_total_volume_dispensed(self):
        response = self.command(":", address=0)
        parts = response.split()
        if len(parts) < 2:
            return response
        return float(parts[0]), parts[1]

    def save_settings(self):
        return self.require_ok(self.command("*", address=0))

    def get_foot_switch_grounded(self):
        return self.command("C", address=0) == self.STATUS_POSITIVE

    def address_for_channel(self, channel):
        if channel is None:
            return self.address
        return channel

    def close(self):
        self.connection.close()


FischerIsotempBath = FisherIsotempBath
MasterflexRegaloICCPump = MasterflexRegloICCPump


def available_usb_ports():
    ports = set()

    for port_info in list_ports.comports():
        searchable_text = " ".join(
            str(value)
            for value in (
                port_info.device,
                port_info.name,
                port_info.description,
                port_info.hwid,
            )
            if value
        ).upper()

        if any(marker in searchable_text for marker in USB_PORT_MARKERS):
            ports.add(port_info.device)

    for pattern in USB_PORT_GLOBS:
        ports.update(glob.glob(pattern))

    return sorted(ports)


def find_devices():
    accumet = None
    pump = None
    ports = available_usb_ports()

    if not ports:
        raise RuntimeError("No USB serial ports found.")

    for port in ports:
        if port_is_open(port):
            print(f"Skipping {port}: already open.")
            continue

        if accumet is None:
            accumet = AccumetMeter.probe(port)
            if accumet is not None:
                print(f"Found Accumet meter on {port}.")
                continue

        if pump is None:
            pump = MasterflexPump.probe(port)
            if pump is not None:
                print(f"Found Masterflex pump on {port}.")

    missing_devices = []
    if accumet is None:
        missing_devices.append("Accumet meter")
    if pump is None:
        missing_devices.append("Masterflex pump")

    if missing_devices:
        close_devices(accumet, pump)
        raise RuntimeError(f"Could not find: {', '.join(missing_devices)}")

    return accumet, pump


def port_is_open(port):
    """Return True if another Linux process already has this device open."""
    if os.name == "nt":
        return False

    target_device = os.path.realpath(port)
    proc_dir = "/proc"

    if not os.path.exists(target_device):
        raise FileNotFoundError(f"Serial port does not exist: {port}")

    if not os.path.isdir(proc_dir):
        return False

    for pid in os.listdir(proc_dir):
        if not pid.isdigit():
            continue

        fd_dir = os.path.join(proc_dir, pid, "fd")
        try:
            for fd in os.listdir(fd_dir):
                fd_path = os.path.join(fd_dir, fd)
                try:
                    if os.path.realpath(fd_path) == target_device:
                        return True
                except OSError:
                    continue
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue

    return False


def open_serial_port(port, **serial_kwargs):
    if port_is_open(port):
        raise RuntimeError(f"Serial port is already open: {port}")

    if os.name != "nt":
        serial_kwargs["exclusive"] = True

    return serial.Serial(port=port, **serial_kwargs)


def log(log_file, message):
    """Log a message to log file. Automatically adds linebreak."""
    with open(log_file, 'a') as f:
        f.write(message + "\n")


def close_devices(accumet=None, pump=None):
    if pump is not None:
        try:
            pump.disable_remote()
        except (OSError, serial.SerialException):
            pass
        finally:
            pump.close()

    if accumet is not None:
        accumet.close()


def run_date_label(date):
    return f"{date.strftime('%B').lower()} {date.day} {date.year}"


def safe_path_label(label):
    label = label.strip()
    label = re.sub(r"[\\/:\*\?\"<>\|]+", " ", label)
    label = re.sub(r"\s+", " ", label)
    return label.strip(" .")


def create_run_paths(resin_name=None):
    os.makedirs(DATA_ROOT, exist_ok=True)
    date_label = run_date_label(datetime.datetime.now())
    resin_label = safe_path_label(resin_name) if resin_name else ""

    run_number = 1
    while True:
        if resin_label:
            run_folder_name = f"{RUN_FOLDER_PREFIX}-{resin_label}-{date_label}-{run_number}"
        else:
            run_folder_name = f"{RUN_FOLDER_PREFIX}-{date_label}-{run_number}"
        run_folder = os.path.join(DATA_ROOT, run_folder_name)
        try:
            os.makedirs(run_folder)
            break
        except FileExistsError:
            run_number += 1

    if resin_label:
        data_filename = f"{run_folder_name}-data.csv"
        log_filename = f"{run_folder_name}-log.txt"
        video_filename = f"{run_folder_name}-output_video.mp4"
        bath_temperature_filename = f"{run_folder_name}-bath_temperature.csv"
        rinse_data_filename = f"{run_folder_name}-rinse_data.csv"
        rinse_bath_temperature_filename = f"{run_folder_name}-rinse_bath_temperature.csv"
    else:
        data_filename = DATA_FILENAME
        log_filename = LOG_FILENAME
        video_filename = VIDEO_FILENAME
        bath_temperature_filename = BATH_TEMPERATURE_FILENAME
        rinse_data_filename = RINSE_DATA_FILENAME
        rinse_bath_temperature_filename = RINSE_BATH_TEMPERATURE_FILENAME

    return {
        "run_folder": run_folder,
        "data_file": os.path.join(run_folder, data_filename),
        "log_file": os.path.join(run_folder, log_filename),
        "video_file": os.path.join(run_folder, video_filename),
        "bath_temperature_file": os.path.join(run_folder, bath_temperature_filename),
        "rinse_data_file": os.path.join(run_folder, rinse_data_filename),
        "rinse_bath_temperature_file": os.path.join(run_folder, rinse_bath_temperature_filename),
    }
