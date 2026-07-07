# originally written by David Brown
# heavily modified by Kyle Wodehouse

import datetime
import time
import serial

from deashing_helpers import (
    FisherIsotempBath,
    MasterflexPump,
    MasterflexRegaloICCPump,
    available_usb_ports,
    create_run_paths,
    find_devices,
    log,
    port_is_open,
)


COND_LIMIT_US_CM = 35
STOP_LIMIT = 50  # Consecutive measurements above the threshold.
MINIMUM_TIME_MINUTES = 180
PUMP_SPEED_RPM = 5.79

CAMERA_INDEX = 0
FRAME_INTERVAL_SECONDS = 60
VIDEO_FPS = 24
BATH_TEMPERATURE_INTERVAL_SECONDS = 60
REGLO_POLL_INTERVAL_SECONDS = 5
REGLO_START_SETTLE_SECONDS = 1
REGLO_TIMEOUT_BUFFER_SECONDS = 300
REGLO_CALIBRATION_SLOPE = 0.1694
REGLO_CALIBRATION_INTERCEPT = -0.727
REGLO_PRIME_VOLUME_ML = 100
REGLO_PRIME_FLOW_RATE_ML_MIN = 5
REGLO_FINAL_VOLUME_ML = 3900
REGLO_FINAL_SPEED_RPM = 100

RUN_TIME_DISPLAY_FORMAT = '%Y-%m-%d %H:%M:%S'
DATA_HEADER = "time,signal,temperature\n"
BATH_TEMPERATURE_HEADER = "time,temperature\n"


def find_isotemp_bath(skip_ports=None):
    if skip_ports is None:
        skip_ports = set()
    else:
        skip_ports = set(skip_ports)

    for port in available_usb_ports():
        if port in skip_ports:
            continue

        if port_is_open(port):
            print(f"Skipping {port}: already open.")
            continue

        bath = FisherIsotempBath.probe(port)
        if bath is None:
            continue

        print(f"Found Fisher Isotemp bath on {port}.")
        return bath

    raise RuntimeError("Could not find Fisher Isotemp bath.")


def find_reglo_icc_pump(skip_ports=None):
    if skip_ports is None:
        skip_ports = set()
    else:
        skip_ports = set(skip_ports)

    for port in available_usb_ports():
        if port in skip_ports:
            continue

        if port_is_open(port):
            print(f"Skipping {port}: already open.")
            continue

        pump = MasterflexRegaloICCPump.probe(port)
        if pump is None:
            continue

        print(f"Found Masterflex Reglo ICC pump on {port}.")
        return pump

    raise RuntimeError("Could not find Masterflex Reglo ICC pump.")


def turn_off_isotemp_bath(bath=None):
    if bath is not None:
        try:
            print(bath.set_unit_off())
            print("Fisher Isotemp bath turned off.")
            return
        finally:
            bath.close()

    bath = find_isotemp_bath()
    try:
        print(bath.set_unit_off())
        print("Fisher Isotemp bath turned off.")
    finally:
        bath.close()


def initialize_data_file(output_file):
    with open(output_file, 'w') as f:
        f.write(DATA_HEADER)


def initialize_rinse_data_file(output_file):
    with open(output_file, 'w') as f:
        f.write(DATA_HEADER)


def initialize_bath_temperature_file(output_file):
    with open(output_file, 'w') as f:
        f.write(BATH_TEMPERATURE_HEADER)


def initialize_rinse_bath_temperature_file(output_file):
    with open(output_file, 'w') as f:
        f.write(BATH_TEMPERATURE_HEADER)


def save_bath_temperature(bath, output_file, log_file):
    timestamp = datetime.datetime.now().strftime(RUN_TIME_DISPLAY_FORMAT)
    temperature = bath.read_temperature()
    with open(output_file, 'a') as f:
        f.write(f"{timestamp},{temperature}\n")
    print(f"{timestamp}: bath temp = {temperature:.2f} C")
    log(log_file, f"{timestamp}, bath temperature = {temperature:.2f} C")


def save_accumet_measurement(measurement, output_file):
    date = measurement["date"]
    hour = measurement["time"]
    cond = measurement["conductivity"]
    temp = measurement["temperature"]
    with open(output_file, 'a') as f:
        f.write(f"{date}{hour},{cond},{temp}\n")


def flow_rate_from_rpm(rpm):
    return REGLO_CALIBRATION_SLOPE * rpm + REGLO_CALIBRATION_INTERCEPT


def rpm_from_flow_rate(flow_rate_ml_min):
    return (flow_rate_ml_min - REGLO_CALIBRATION_INTERCEPT) / REGLO_CALIBRATION_SLOPE


def maybe_save_bath_temperature_during_flush(
    bath,
    bath_temperature_file,
    log_file,
    last_bath_temperature_time,
):
    current_time = time.time()
    if bath is None or current_time - last_bath_temperature_time < BATH_TEMPERATURE_INTERVAL_SECONDS:
        return last_bath_temperature_time

    try:
        save_bath_temperature(bath, bath_temperature_file, log_file)
    except (OSError, RuntimeError, serial.SerialException, UnicodeDecodeError, ValueError) as error:
        print(f"Warning: error while reading Fisher Isotemp bath temperature: {error}")
        log(log_file, f"{datetime.datetime.now()}, error reading bath temperature: {error}")

    return current_time


def wait_for_reglo_dispense(
    reglo_pump,
    expected_seconds,
    description,
    bath=None,
    bath_temperature_file=None,
    accumet=None,
    rinse_data_file=None,
    log_file=None,
):
    deadline = time.monotonic() + expected_seconds + REGLO_TIMEOUT_BUFFER_SECONDS
    last_bath_temperature_time = time.time()
    while time.monotonic() < deadline:
        if not reglo_pump.get_running():
            print(f"Finished {description}.")
            return
        if bath_temperature_file is not None and log_file is not None:
            last_bath_temperature_time = maybe_save_bath_temperature_during_flush(
                bath,
                bath_temperature_file,
                log_file,
                last_bath_temperature_time,
            )
        if accumet is not None and rinse_data_file is not None:
            measurement = accumet.read_measurement()
            if measurement is not None:
                save_accumet_measurement(measurement, rinse_data_file)
        time.sleep(REGLO_POLL_INTERVAL_SECONDS)

    try:
        reglo_pump.stop()
    finally:
        raise TimeoutError(f"Timed out waiting for Reglo ICC pump to finish {description}.")


def dispense_reglo_volume_at_rpm(
    reglo_pump,
    volume_ml,
    rpm,
    description,
    bath=None,
    bath_temperature_file=None,
    accumet=None,
    rinse_data_file=None,
    log_file=None,
):
    flow_rate_ml_min = flow_rate_from_rpm(rpm)
    expected_seconds = (volume_ml / flow_rate_ml_min) * 60
    print(f"Starting {description}: {volume_ml} mL at {rpm:.2f} RPM.")
    reglo_pump.set_volume_at_rate_mode()
    reglo_pump.require_ok(reglo_pump.set_volume_ml(volume_ml))
    reglo_pump.set_speed_rpm(rpm)
    reglo_pump.start()
    time.sleep(REGLO_START_SETTLE_SECONDS)
    wait_for_reglo_dispense(
        reglo_pump,
        expected_seconds,
        description,
        bath=bath,
        bath_temperature_file=bath_temperature_file,
        accumet=accumet,
        rinse_data_file=rinse_data_file,
        log_file=log_file,
    )


def dispense_reglo_volume_at_flow_rate(
    reglo_pump,
    volume_ml,
    flow_rate_ml_min,
    description,
    bath=None,
    bath_temperature_file=None,
    accumet=None,
    rinse_data_file=None,
    log_file=None,
):
    rpm = rpm_from_flow_rate(flow_rate_ml_min)
    print(
        f"Starting {description}: {volume_ml} mL at {flow_rate_ml_min} mL/min "
        f"({rpm:.2f} RPM from calibration)."
    )
    dispense_reglo_volume_at_rpm(
        reglo_pump,
        volume_ml,
        rpm,
        description,
        bath=bath,
        bath_temperature_file=bath_temperature_file,
        accumet=accumet,
        rinse_data_file=rinse_data_file,
        log_file=log_file,
    )


def run_post_run_reglo_flush(
    skip_ports=None,
    bath=None,
    bath_temperature_file=None,
    accumet=None,
    rinse_data_file=None,
    log_file=None,
):
    reglo_pump = find_reglo_icc_pump(skip_ports=skip_ports)
    try:
        if log_file is not None:
            log(log_file, f"{datetime.datetime.now()}, starting Masterflex Reglo ICC post-run flush...")
        if accumet is not None:
            accumet.reset_input_buffer()
        dispense_reglo_volume_at_flow_rate(
            reglo_pump,
            REGLO_PRIME_VOLUME_ML,
            REGLO_PRIME_FLOW_RATE_ML_MIN,
            "100 mL Reglo ICC dispense",
            bath=bath,
            bath_temperature_file=bath_temperature_file,
            accumet=accumet,
            rinse_data_file=rinse_data_file,
            log_file=log_file,
        )
        dispense_reglo_volume_at_rpm(
            reglo_pump,
            REGLO_FINAL_VOLUME_ML,
            REGLO_FINAL_SPEED_RPM,
            "3900 mL Reglo ICC dispense",
            bath=bath,
            bath_temperature_file=bath_temperature_file,
            accumet=accumet,
            rinse_data_file=rinse_data_file,
            log_file=log_file,
        )
        if log_file is not None:
            log(log_file, f"{datetime.datetime.now()}, completed Masterflex Reglo ICC post-run flush...")
    finally:
        try:
            reglo_pump.stop()
        except (OSError, RuntimeError, serial.SerialException, UnicodeDecodeError, ValueError):
            pass
        reglo_pump.close()


def main():
    import cv2

    resin_name = input("What resin is being used? ").strip()
    run_paths = create_run_paths(resin_name)
    output_file = run_paths["data_file"]
    log_file = run_paths["log_file"]
    video_file = run_paths["video_file"]
    bath_temperature_file = run_paths["bath_temperature_file"]
    rinse_data_file = run_paths["rinse_data_file"]
    rinse_bath_temperature_file = run_paths["rinse_bath_temperature_file"]
    initialize_data_file(output_file)
    initialize_bath_temperature_file(bath_temperature_file)
    initialize_rinse_data_file(rinse_data_file)
    initialize_rinse_bath_temperature_file(rinse_bath_temperature_file)

    print(f"Saving run data in {run_paths['run_folder']}")
    print(f"Saving run video to {video_file}")
    print(f"Saving bath temperature data to {bath_temperature_file}")
    print(f"Saving rinse data to {rinse_data_file}")
    print(f"Saving rinse bath temperature data to {rinse_bath_temperature_file}")

    grace_period = MINIMUM_TIME_MINUTES
    hard_stop = float(input("Maximum Time (min): "))

    accumet = None
    pump = None
    cap = None
    video = None
    bath = None

    try:
        accumet, pump = find_devices()
        bath = find_isotemp_bath(skip_ports=(accumet.port, pump.port))
        accumet.set_csv_output()
        pump.enable_remote()
        pump.set_speed_rpm(PUMP_SPEED_RPM)

        cap = cv2.VideoCapture(CAMERA_INDEX)
        if not cap.isOpened():
            raise RuntimeError("Could not open camera.")

        print("Camera started.")

        pump.start()
        accumet.reset_input_buffer()

        min_time = datetime.datetime.now() + datetime.timedelta(minutes=grace_period)
        max_time = datetime.datetime.now() + datetime.timedelta(minutes=hard_stop)
        print(f"Will start checking conductivity until {min_time.strftime(RUN_TIME_DISPLAY_FORMAT)}")
        print(f"Hard stop at {max_time.strftime(RUN_TIME_DISPLAY_FORMAT)}")

        log(log_file, f"Starting run at {datetime.datetime.now()}...")
        log(log_file, f"Resin: {resin_name}")

        last_frame_time = 0
        last_bath_temperature_time = 0
        stop_count = 0
        running = True
        last_measurement = None

        while running:
            measurement = accumet.read_measurement()
            if measurement is not None:
                last_measurement = measurement
                date = measurement["date"]
                hour = measurement["time"]
                cond = measurement["conductivity"]
                temp = measurement["temperature"]

                print(f"{date} {hour}: cond = {cond:.3f} uS/cm; temp = {temp:.2f} C")

                if cond > COND_LIMIT_US_CM and datetime.datetime.now() > min_time:
                    stop_count += 1
                    log(log_file, f"{date}{hour}, exceeded conductivity limit {stop_count} time(s)")
                    if stop_count >= STOP_LIMIT:
                        running = False
                        print("Stopping pump")
                else:
                    stop_count = 0

                try:
                    with open(output_file, 'a') as f:
                        f.write(f"{date}{hour},{cond},{temp}\n")
                except OSError:
                    print("Error saving data to file")
                    log(log_file, f"{date}{hour}, error saving data to file...")

            current_time = time.time()
            if current_time - last_frame_time >= FRAME_INTERVAL_SECONDS:
                ret, frame = cap.read()
                if ret:
                    frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
                    if video is None:
                        height, width, _channels = frame.shape
                        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                        video = cv2.VideoWriter(video_file, fourcc, VIDEO_FPS, (width, height))
                        if not video.isOpened():
                            raise RuntimeError(f"Could not open video file for writing: {video_file}")

                    video.write(frame)
                    print(f"Appended frame to video: {video_file}")
                    last_frame_time = current_time
                else:
                    print("Warning: Camera failed to grab frame.")

            if current_time - last_bath_temperature_time >= BATH_TEMPERATURE_INTERVAL_SECONDS:
                try:
                    save_bath_temperature(bath, bath_temperature_file, log_file)
                except (OSError, RuntimeError, serial.SerialException, UnicodeDecodeError, ValueError) as error:
                    print(f"Warning: error while reading Fisher Isotemp bath temperature: {error}")
                    log(log_file, f"{datetime.datetime.now()}, error reading bath temperature: {error}")
                finally:
                    last_bath_temperature_time = current_time

            if datetime.datetime.now() > max_time:
                print("Reached hard stop")
                running = False
                if last_measurement is None:
                    log(log_file, f"{datetime.datetime.now()}, reached end time limit (hard stop)...")
                else:
                    log(
                        log_file,
                        f"{last_measurement['date']}{last_measurement['time']}, "
                        "reached end time limit (hard stop)...",
                    )

    finally:
        if pump is not None:
            try:
                time.sleep(MasterflexPump.COMMAND_DELAY_SECONDS)
                print(pump.stop())
                time.sleep(MasterflexPump.COMMAND_DELAY_SECONDS)
                print(pump.disable_remote())
                time.sleep(MasterflexPump.COMMAND_DELAY_SECONDS)
            except (OSError, serial.SerialException) as error:
                print(f"Warning: error while stopping pump: {error}")
            finally:
                pump.close()

        if cap is not None:
            cap.release()
            cv2.destroyAllWindows()
            print("Camera released.")

        if video is not None:
            video.release()
            print("Video saved.")

        try:
            skip_ports = (bath.port,) if bath is not None else None
            run_post_run_reglo_flush(
                skip_ports=skip_ports,
                bath=bath,
                bath_temperature_file=rinse_bath_temperature_file,
                accumet=accumet,
                rinse_data_file=rinse_data_file,
                log_file=log_file,
            )
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            serial.SerialException,
            UnicodeDecodeError,
            ValueError,
        ) as error:
            print(f"Warning: error while running Masterflex Reglo ICC post-run flush: {error}")

        if accumet is not None:
            accumet.close()
            accumet = None

        try:
            turn_off_isotemp_bath(bath)
            bath = None
        except (OSError, RuntimeError, serial.SerialException, UnicodeDecodeError, ValueError) as error:
            print(f"Warning: error while turning off Fisher Isotemp bath: {error}")

        print("Run complete.")


if __name__ == "__main__":
    main()
