#!/usr/bin/env python3
"""Probe Reglo ICC serial handshakes without starting any pump channel.

The Reglo ICC manual documents the protocol-version request as ``0x!<CR>``.
This script also tries the shorter ``0!<CR>`` form used by the current driver,
several address variants, and both documented message terminators.  Every
request and response is recorded as raw Python byte literals so the transcript
can be used to update the production driver.

Examples:
    python3 reglo_icc_handshake_probe.py
    python3 reglo_icc_handshake_probe.py --port /dev/ttyACM0
    python3 reglo_icc_handshake_probe.py --port COM5 --try-setup

The default survey is read-only and never sends start, stop, speed, direction,
or dispense commands.  ``--try-setup`` additionally tests address and channel
mode configuration, but only on a port that first returned a strong ICC
response on an OS port named ISMATEC.
"""

import argparse
import datetime
import os
import platform
import re
import sys
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Set, Tuple

try:
    import serial
    from serial.tools import list_ports
except ModuleNotFoundError:
    serial = None
    list_ports = None


DEFAULT_BAUDRATE = 9600
DEFAULT_RESPONSE_TIMEOUT_SECONDS = 0.8
DEFAULT_IDLE_TIMEOUT_SECONDS = 0.12
DEFAULT_OPEN_DELAY_SECONDS = 0.25
ISMATEC_MARKER = "ismatec"

TERMINATORS = (
    ("CR", b"\r"),
    ("CRLF", b"\r\n"),
)

# These are all queries.  None can start or otherwise move a pump channel.
READ_ONLY_REQUESTS = (
    (
        "documented protocol-version query at per-pump address 0",
        b"0x!",
        "protocol",
    ),
) + tuple(
    (
        f"documented protocol-version query at legacy pump address {address}",
        f"{address}x!".encode("ascii"),
        "protocol",
    )
    for address in range(1, 9)
) + (
    (
        "current-driver short query at address 0 (bare ! is a flow query in v2)",
        b"0!",
        "short-query",
    ),
    (
        "current-driver short query at address 1 (bare ! is a flow query in v2)",
        b"1!",
        "short-query",
    ),
    (
        "protocol-version query without an address",
        b"x!",
        "protocol",
    ),
    ("channel-addressing-mode query at address 0", b"0~", "mode"),
    ("channel-addressing-mode query at address 1", b"1~", "mode"),
    ("pump serial-number query at address 0", b"0xS", "serial-number"),
)

# These commands do not move a channel, but they can change pump configuration.
# They are only sent with the explicit --try-setup option and only after a
# protocol-version query has been received on an OS port named ISMATEC.
SETUP_REQUESTS = (
    ("assign pump address 1", b"@1", "setup"),
    ("enable channel addressing through pump address 1", b"1~1", "setup"),
    ("enable channel addressing through placeholder address 0", b"0~1", "setup"),
    ("query channel-addressing mode at address 0", b"0~", "mode"),
    ("query channel-addressing mode at address 1", b"1~", "mode"),
)


@dataclass(frozen=True)
class ProbeCase:
    label: str
    request: bytes
    category: str
    terminator_name: str
    terminator: bytes


@dataclass(frozen=True)
class ProbeResult:
    port: str
    baudrate: int
    case: ProbeCase
    response: bytes
    classification: str
    protocol_match: bool
    fingerprint_signal: bool


class Transcript:
    def __init__(self, filename: str):
        self.filename = filename
        self._file = open(filename, "w", encoding="utf-8")

    def write(self, message: str = "") -> None:
        print(message, flush=True)
        self._file.write(message + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def build_cases(
    requests: Sequence[Tuple[str, bytes, str]],
    terminators: Sequence[Tuple[str, bytes]] = TERMINATORS,
) -> List[ProbeCase]:
    cases = []
    for terminator_name, terminator in terminators:
        for label, request, category in requests:
            cases.append(
                ProbeCase(
                    label=label,
                    request=request + terminator,
                    category=category,
                    terminator_name=terminator_name,
                    terminator=terminator,
                )
            )
    return cases


def build_fingerprint_requests(address: int) -> Tuple[Tuple[str, bytes, str], ...]:
    """Build read-only ICC identity queries for a proven pump address."""
    address_text = str(address)
    return (
        (
            f"ICC event-message state query at address {address}",
            f"{address_text}xE".encode("ascii"),
            "fingerprint-event-state",
        ),
        (
            f"ICC channel-count query at address {address}",
            f"{address_text}xA".encode("ascii"),
            "fingerprint-channel-count",
        ),
        (
            f"ICC serial-number query at address {address}",
            f"{address_text}xS".encode("ascii"),
            "fingerprint-serial-number",
        ),
    )


def response_lines(response: bytes) -> List[bytes]:
    return [line.strip() for line in re.split(br"[\r\n]+", response) if line.strip()]


def classify_response(case: ProbeCase, response: bytes) -> Tuple[str, bool, bool]:
    lines = response_lines(response)
    if not lines:
        return "no response", False, False

    if case.category == "protocol" and lines == [b"2"]:
        return "protocol-version 2 signal", True, True

    if case.category == "short-query":
        return "response to bare ! query (not an ICC identity signal)", False, False

    if case.category == "fingerprint-event-state" and len(lines) == 1 and lines[0] in (b"0", b"1"):
        return "ICC fingerprint signal: event-message state", False, True

    if case.category == "fingerprint-channel-count":
        try:
            channel_count = int(lines[0])
        except ValueError:
            channel_count = 0
        if len(lines) == 1 and 1 <= channel_count <= 4:
            return "ICC fingerprint signal: channel count", False, True

    if case.category == "fingerprint-serial-number":
        if len(lines) == 1 and lines[0] not in (b"*", b"#", b"+", b"-"):
            if all(0x20 <= byte <= 0x7E for byte in lines[0]):
                return "ICC fingerprint signal: serial number", False, True

    if len(lines) == 1 and lines[0] in (b"*", b"#", b"+", b"-"):
        names = {
            b"*": "command accepted",
            b"#": "command rejected",
            b"+": "positive status",
            b"-": "negative status",
        }
        return names[lines[0]], False, False

    if case.category == "mode" and any(line in (b"0", b"1", b"2") for line in lines):
        return "valid channel-addressing mode response", False, False

    if any(byte < 0x09 or (0x0E <= byte < 0x20) or byte > 0x7E for byte in response):
        return "non-ASCII/binary response (probably another device or wrong baud)", False, False

    return "printable response", False, False


def read_until_idle(
    connection,
    response_timeout: float,
    idle_timeout: float,
) -> bytes:
    chunks = []
    deadline = time.monotonic() + response_timeout
    last_data_time = None

    while time.monotonic() < deadline:
        now = time.monotonic()
        if last_data_time is not None and now - last_data_time >= idle_timeout:
            break

        waiting = connection.in_waiting
        chunk = connection.read(waiting or 1)
        if chunk:
            chunks.append(chunk)
            last_data_time = time.monotonic()

    return b"".join(chunks)


def open_connection(port: str, baudrate: int, timeout: float):
    return serial.Serial(
        port=port,
        baudrate=baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=min(0.05, timeout),
        write_timeout=timeout,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )


def run_case(
    connection,
    case: ProbeCase,
    response_timeout: float,
    idle_timeout: float,
) -> Tuple[bytes, str, bool, bool]:
    connection.reset_input_buffer()
    connection.write(case.request)
    connection.flush()
    response = read_until_idle(connection, response_timeout, idle_timeout)
    classification, protocol_match, fingerprint_signal = classify_response(case, response)
    return response, classification, protocol_match, fingerprint_signal


def describe_port(port_info) -> str:
    details = [port_info.device]
    if port_info.description and port_info.description != "n/a":
        details.append(port_info.description)
    if port_info.hwid and port_info.hwid != "n/a":
        details.append(port_info.hwid)
    return " | ".join(details)


def port_metadata(port_info) -> str:
    fields = (
        port_info.device,
        port_info.description,
        getattr(port_info, "manufacturer", None),
        getattr(port_info, "product", None),
        port_info.hwid,
    )
    return " ".join(str(field) for field in fields if field and field != "n/a").lower()


def is_ismatec_port(port_info) -> bool:
    return ISMATEC_MARKER in port_metadata(port_info)


def discover_ports(
    requested_ports: Optional[Sequence[str]],
    all_ports: bool = False,
) -> Tuple[List[str], List[str], Set[str]]:
    available = sorted(list_ports.comports(), key=lambda info: info.device)
    ismatec_devices = {info.device for info in available if is_ismatec_port(info)}

    if requested_ports:
        ports = []
        for port in requested_ports:
            if port not in ports:
                ports.append(port)
        return ports, [describe_port(info) for info in available], ismatec_devices

    if not all_ports:
        ismatec_ports = [info for info in available if is_ismatec_port(info)]
        if ismatec_ports:
            return (
                [info.device for info in ismatec_ports],
                [describe_port(info) for info in ismatec_ports],
                ismatec_devices,
            )

    return [info.device for info in available], [describe_port(info) for info in available], ismatec_devices


def unique_positive_ints(values: Iterable[int]) -> List[int]:
    result = []
    for value in values:
        if value <= 0:
            raise ValueError("Baud rates must be positive integers")
        if value not in result:
            result.append(value)
    return result


def probe_transport(
    port: str,
    baudrate: int,
    cases: Sequence[ProbeCase],
    transcript: Transcript,
    response_timeout: float,
    idle_timeout: float,
    open_delay: float,
) -> List[ProbeResult]:
    results = []
    transcript.write()
    transcript.write(f"=== Port {port} at {baudrate} baud, 8-N-1, no flow control ===")

    connection = None
    try:
        connection = open_connection(port, baudrate, response_timeout)
        time.sleep(open_delay)

        initial_bytes = read_until_idle(connection, min(response_timeout, 0.3), idle_timeout)
        if initial_bytes:
            transcript.write(f"Initial unsolicited bytes: {initial_bytes!r}")

        for case in cases:
            transcript.write(f"TX {case.label} [{case.terminator_name}]: {case.request!r}")
            try:
                response, classification, protocol_match, fingerprint_signal = run_case(
                    connection,
                    case,
                    response_timeout,
                    idle_timeout,
                )
            except (OSError, serial.SerialException, serial.SerialTimeoutException) as error:
                transcript.write(f"ERROR during request: {type(error).__name__}: {error}")
                break

            transcript.write(f"RX: {response!r}")
            transcript.write(f"Result: {classification}")
            results.append(
                ProbeResult(
                    port=port,
                    baudrate=baudrate,
                    case=case,
                    response=response,
                    classification=classification,
                    protocol_match=protocol_match,
                    fingerprint_signal=fingerprint_signal,
                )
            )
    except (OSError, serial.SerialException) as error:
        transcript.write(f"Could not open port: {type(error).__name__}: {error}")
    finally:
        if connection is not None and connection.is_open:
            connection.close()

    return results


def address_for_case(case: ProbeCase) -> int:
    """Return the addressed-pump digit used by a protocol query."""
    if case.request[:1].isdigit():
        return int(case.request[:1])
    return 0


def choose_fingerprint_transports(matches: Sequence[ProbeResult]) -> List[ProbeResult]:
    """Choose one protocol-match address per physical transport."""
    chosen = []
    seen = set()
    for match in matches:
        key = (match.port, match.baudrate, match.case.terminator_name)
        if key not in seen:
            chosen.append(match)
            seen.add(key)
    return chosen


def run_fingerprint_probes(
    protocol_matches: Sequence[ProbeResult],
    transcript: Transcript,
    response_timeout: float,
    idle_timeout: float,
    open_delay: float,
) -> List[ProbeResult]:
    results = []
    transcript.write()
    transcript.write("=== ICC-specific fingerprint probes ===")
    transcript.write(
        "A protocol-version reply alone is not treated as unique. "
        "Each candidate must also answer ICC event-state, channel-count, "
        "and serial-number queries."
    )

    for match in choose_fingerprint_transports(protocol_matches):
        address = address_for_case(match.case)
        cases = build_cases(
            build_fingerprint_requests(address),
            ((match.case.terminator_name, match.case.terminator),),
        )
        results.extend(
            probe_transport(
                match.port,
                match.baudrate,
                cases,
                transcript,
                response_timeout,
                idle_timeout,
                open_delay,
            )
        )
    return results


def identify_ismatec_protocol_matches(
    results: Sequence[ProbeResult],
    ismatec_ports: Set[str],
) -> List[ProbeResult]:
    """Return protocol matches on ports named ISMATEC by the OS."""
    strong = []
    seen = set()
    for result in results:
        key = (
            result.port,
            result.baudrate,
            result.case.terminator_name,
            address_for_case(result.case),
        )
        if result.protocol_match and result.port in ismatec_ports:
            transport_key = (result.port, result.baudrate, result.case.terminator_name)
            if transport_key not in seen:
                strong.append(result)
                seen.add(transport_key)
    return strong


def choose_setup_transports(matches: Sequence[ProbeResult]) -> List[ProbeResult]:
    """Choose one ISMATEC protocol match per physical transport."""
    chosen = []
    seen = set()
    for match in matches:
        key = match.port
        if key not in seen:
            chosen.append(match)
            seen.add(key)
    return chosen


def run_setup_probes(
    matches: Sequence[ProbeResult],
    transcript: Transcript,
    response_timeout: float,
    idle_timeout: float,
    open_delay: float,
) -> List[ProbeResult]:
    results = []
    transcript.write()
    transcript.write("=== Optional non-motion setup probes ===")
    transcript.write(
        "These requests can assign pump address 1 and enable independent channel addressing."
    )

    for match in choose_setup_transports(matches):
        setup_cases = build_cases(
            SETUP_REQUESTS,
            ((match.case.terminator_name, match.case.terminator),),
        )
        results.extend(
            probe_transport(
                match.port,
                match.baudrate,
                setup_cases,
                transcript,
                response_timeout,
                idle_timeout,
                open_delay,
            )
        )
    return results


def print_summary(
    results: Sequence[ProbeResult],
    transcript: Transcript,
    script_name: str,
    ismatec_ports: Set[str],
) -> List[ProbeResult]:
    protocol_matches = [result for result in results if result.protocol_match]
    strong_matches = identify_ismatec_protocol_matches(results, ismatec_ports)
    responsive = [result for result in results if result.response]

    transcript.write()
    transcript.write("=== Summary ===")
    if strong_matches:
        transcript.write("Confirmed ICC handshakes (OS name ISMATEC + protocol version 2):")
        for match in strong_matches:
            transcript.write(
                f"- port={match.port} baud={match.baudrate} "
                f"address={address_for_case(match.case)} "
                f"terminator={match.case.terminator_name} request={match.case.request!r} "
                f"response={match.response!r}"
            )

        strong_ports = sorted({match.port for match in strong_matches})
        transcript.write()
        if len(strong_ports) == 1:
            best = strong_matches[0]
            transcript.write("Recommended focused setup test:")
            transcript.write(
                f"  python3 {script_name} --port {best.port!r} "
                f"--baudrate {best.baudrate} --try-setup"
            )
        else:
            transcript.write(
                "AMBIGUOUS: multiple ISMATEC-named physical ports passed the protocol check. "
                "Select exactly one intended port before using --try-setup."
            )
            transcript.write("Candidate ports: " + ", ".join(str(port) for port in strong_ports))
    else:
        if protocol_matches:
            transcript.write(
                "Protocol-version 2 replies were observed, but no transport passed the "
                "ISMATEC-name + protocol-version handshake. Do not configure a pump "
                "based on protocol replies alone."
            )
            for match in choose_fingerprint_transports(protocol_matches):
                transcript.write(
                    f"- protocol-like response: port={match.port} baud={match.baudrate} "
                    f"address={address_for_case(match.case)} response={match.response!r}"
                )
        else:
            transcript.write("No request returned an exact protocol-version 2 response.")
        if responsive:
            transcript.write("Ports with any response (inspect their raw RX lines above):")
            seen = set()
            for result in responsive:
                key = (result.port, result.baudrate)
                if key not in seen:
                    transcript.write(f"- port={result.port} baud={result.baudrate}")
                    seen.add(key)
        else:
            transcript.write("No tested port returned any bytes.")
            transcript.write(
                "Check the selected OS port, cable/interface, pump power, and 9600 8-N-1 settings."
            )

    return strong_matches


def default_log_filename() -> str:
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"reglo_icc_handshake_{timestamp}.log"


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Test non-motion Reglo ICC transport and device-identity variants and log exact bytes. "
            "Without --port, ports named ISMATEC are selected automatically."
        )
    )
    parser.add_argument(
        "--port",
        action="append",
        help=(
            "Serial port to test; repeat to test multiple ports "
            "(default: automatically selected ISMATEC-named ports)."
        ),
    )
    parser.add_argument(
        "--all-ports",
        action="store_true",
        help="Survey every detected port instead of prioritizing ISMATEC-named ports.",
    )
    parser.add_argument(
        "--baudrate",
        action="append",
        type=int,
        help="Baud rate to test; repeat for multiple values (default: 9600).",
    )
    parser.add_argument(
        "--response-timeout",
        type=float,
        default=DEFAULT_RESPONSE_TIMEOUT_SECONDS,
        help="Maximum seconds to wait for each response (default: %(default)s).",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=DEFAULT_IDLE_TIMEOUT_SECONDS,
        help="Stop reading after this many quiet seconds (default: %(default)s).",
    )
    parser.add_argument(
        "--open-delay",
        type=float,
        default=DEFAULT_OPEN_DELAY_SECONDS,
        help="Seconds to wait after opening each port (default: %(default)s).",
    )
    parser.add_argument(
        "--try-setup",
        action="store_true",
        help=(
            "After an ISMATEC + protocol-version-2 match, test non-motion @1 and channel-addressing "
            "setup commands. This can change pump configuration."
        ),
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Transcript path (default: timestamped file in the current directory).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the request matrix without opening a serial port.",
    )
    return parser


def validate_timeouts(args, parser: argparse.ArgumentParser) -> None:
    for name in ("response_timeout", "idle_timeout", "open_delay"):
        value = getattr(args, name)
        if value < 0 or (name != "open_delay" and value == 0):
            parser.error(f"--{name.replace('_', '-')} must be greater than zero")
    if args.idle_timeout > args.response_timeout:
        parser.error("--idle-timeout cannot exceed --response-timeout")


def dry_run(cases: Sequence[ProbeCase], include_setup: bool) -> int:
    print("Read-only request matrix (no serial port will be opened):")
    for case in cases:
        print(f"- {case.label} [{case.terminator_name}]: {case.request!r}")
    print(
        "After any exact protocol-version 2 response, the script also queries "
        "xE, xA, and xS at that same address and terminator to build an ICC fingerprint."
    )
    if include_setup:
        print("Optional setup matrix (would only run after an ISMATEC + protocol-version-2 match):")
        for case in build_cases(SETUP_REQUESTS):
            print(f"- {case.label} [{case.terminator_name}]: {case.request!r}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    validate_timeouts(args, parser)

    try:
        baudrates = unique_positive_ints(args.baudrate or [DEFAULT_BAUDRATE])
    except ValueError as error:
        parser.error(str(error))

    read_only_cases = build_cases(READ_ONLY_REQUESTS)
    if args.dry_run:
        return dry_run(read_only_cases, args.try_setup)

    if serial is None or list_ports is None:
        parser.error("pyserial is required; install it with: python3 -m pip install pyserial")

    ports, descriptions, ismatec_ports = discover_ports(args.port, args.all_ports)
    if not ports:
        parser.error("No serial ports were detected; use --port to provide one explicitly")

    log_filename = args.log_file or default_log_filename()
    transcript = Transcript(log_filename)
    try:
        transcript.write("Reglo ICC handshake probe")
        transcript.write(f"Started: {datetime.datetime.now().isoformat(timespec='seconds')}")
        transcript.write(f"Host: {platform.platform()}")
        transcript.write(f"Python: {sys.version.split()[0]}")
        transcript.write(f"pyserial: {getattr(serial, '__version__', 'unknown')}")
        transcript.write(f"Working directory: {os.getcwd()}")
        transcript.write(f"Log file: {os.path.abspath(log_filename)}")
        transcript.write("Detected serial ports:")
        for description in descriptions:
            transcript.write(f"- {description}")
        transcript.write(f"OS metadata ports containing ISMATEC: {sorted(ismatec_ports)!r}")
        transcript.write(f"Ports selected for survey: {ports!r}")
        transcript.write(f"Baud rates selected for survey: {baudrates!r}")
        transcript.write(
            "Safety: the survey and ICC fingerprint contain queries only; --try-setup "
            "can change address/channel configuration but never sends pump-motion commands."
        )

        results = []
        for port in ports:
            for baudrate in baudrates:
                results.extend(
                    probe_transport(
                        port,
                        baudrate,
                        read_only_cases,
                        transcript,
                        args.response_timeout,
                        args.idle_timeout,
                        args.open_delay,
                    )
                )

        protocol_matches = [result for result in results if result.protocol_match]
        results.extend(
            run_fingerprint_probes(
                protocol_matches,
                transcript,
                args.response_timeout,
                args.idle_timeout,
                args.open_delay,
            )
        )

        matches = print_summary(
            results,
            transcript,
            os.path.basename(__file__),
            ismatec_ports,
        )

        if args.try_setup:
            strong_ports = {match.port for match in matches}
            if len(strong_ports) == 1:
                run_setup_probes(
                    matches,
                    transcript,
                    args.response_timeout,
                    args.idle_timeout,
                    args.open_delay,
                )
            else:
                transcript.write()
                if not strong_ports:
                    transcript.write(
                        "Setup probes skipped: no ISMATEC-named port returned protocol version 2."
                    )
                else:
                    transcript.write(
                        "Setup probes skipped: multiple ISMATEC-named physical ports passed; "
                        "select exactly one with --port."
                    )

        transcript.write()
        transcript.write(f"Finished. Share this transcript: {os.path.abspath(log_filename)}")
        return 0 if matches else 1
    except KeyboardInterrupt:
        transcript.write()
        transcript.write("Interrupted by user; any open serial connection was closed.")
        return 130
    finally:
        transcript.close()


if __name__ == "__main__":
    raise SystemExit(main())
