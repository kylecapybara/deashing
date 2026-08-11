# Sugar deashing python script
# written by Kyle Wodehouse & David Brown

import datetime
import os
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
STOP_LIMIT = 50  # consecutive measurements above the threshold.
MINIMUM_TIME_MINUTES = 180
PUMP_SPEED_RPM = 5.79

CAMERA_INDEX = 0
FRAME_INTERVAL_SECONDS = 60
VIDEO_FPS = 24
IMAGE_INTERVAL_SECONDS = 20
IMAGE_CAPTURE_DURATION_SECONDS = 2 * 60 * 60
BATH_TEMPERATURE_INTERVAL_SECONDS = 60
REGLO_POLL_INTERVAL_SECONDS = 5
REGLO_START_SETTLE_SECONDS = 1
REGLO_TIMEOUT_BUFFER_SECONDS = 300
REGLO_CALIBRATION_SLOPE = 0.1694
REGLO_CALIBRATION_INTERCEPT = -0.727

## post run rinse steps !!
REGLO_RINSE_STEPS = (
    {
        "description": "100 mL channel 1 water rinse",
        "channel": 1,
        "volume_ml": 100,
        "flow_rate_ml_min": 5,
    },
    {
        "description": "1900 mL channel 1 water rinse",
        "channel": 1,
        "volume_ml": 1900,
        "rpm": 100,
    },
)

RUN_TIME_DISPLAY_FORMAT = '%Y-%m-%d %H:%M:%S'
DATA_HEADER = "time,signal,temperature\n"
BATH_TEMPERATURE_HEADER = "time,temperature\n"


def prompt_yes_no_default_yes(prompt):
    while True:
        try:
            response = input(f"{prompt} [Y/n]: ").strip().lower()
        except EOFError:
            print()
            return True

        if response in ("", "y", "yes"):
            return True
        if response in ("n", "no"):
            return False

        print("Please enter yes or no. Press Enter for yes.")


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


def save_bath_temperature(bath, output_file):
    timestamp = datetime.datetime.now().strftime(RUN_TIME_DISPLAY_FORMAT)
    temperature = bath.read_temperature()
    with open(output_file, 'a') as f:
        f.write(f"{timestamp},{temperature}\n")
    print(f"{timestamp}: bath temp = {temperature:.2f} C")


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


def maybe_save_bath_temperature_during_flush(bath, bath_temperature_file, log_file, last_bath_temperature_time):
    current_time = time.time()
    if bath is None or current_time - last_bath_temperature_time < BATH_TEMPERATURE_INTERVAL_SECONDS:
        return last_bath_temperature_time

    try:
        save_bath_temperature(bath, bath_temperature_file)
    except (OSError, RuntimeError, serial.SerialException, UnicodeDecodeError, ValueError) as error:
        print(f"Warning: error while reading Fisher Isotemp bath temperature: {error}")
        log(log_file, f"{datetime.datetime.now()}, error reading bath temperature: {error}")

    return current_time


def wait_for_reglo_dispense(reglo_pump, expected_seconds, description, channel, bath=None, bath_temperature_file=None, accumet=None, rinse_data_file=None, log_file=None):
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
        reglo_pump.stop(channel=channel)
    finally:
        raise TimeoutError(f"Timed out waiting for Reglo ICC pump to finish {description}.")


def dispense_reglo_volume_at_rpm(reglo_pump, volume_ml, rpm, description, channel, bath=None, bath_temperature_file=None, accumet=None, rinse_data_file=None, log_file=None):
    flow_rate_ml_min = flow_rate_from_rpm(rpm)
    expected_seconds = (volume_ml / flow_rate_ml_min) * 60
    print(
        f"Starting {description}: channel {channel}, {rpm:.2f} RPM for "
        f"{expected_seconds:.1f} seconds (calculated for {volume_ml} mL)."
    )
    reglo_pump.set_time_mode(channel=channel)
    reglo_pump.set_speed_rpm(rpm, channel=channel)
    reglo_pump.set_run_time_seconds(expected_seconds, channel=channel)
    reglo_pump.start(channel=channel)
    time.sleep(REGLO_START_SETTLE_SECONDS)
    wait_for_reglo_dispense(
        reglo_pump,
        expected_seconds,
        description,
        channel,
        bath=bath,
        bath_temperature_file=bath_temperature_file,
        accumet=accumet,
        rinse_data_file=rinse_data_file,
        log_file=log_file,
    )


def dispense_reglo_volume_at_flow_rate(reglo_pump, volume_ml, flow_rate_ml_min, description, channel, bath=None, bath_temperature_file=None, accumet=None, rinse_data_file=None, log_file=None):
    rpm = rpm_from_flow_rate(flow_rate_ml_min)
    print(
        f"Starting {description}: channel {channel}, {volume_ml} mL at {flow_rate_ml_min} mL/min "
        f"({rpm:.2f} RPM from calibration)."
    )
    dispense_reglo_volume_at_rpm(
        reglo_pump,
        volume_ml,
        rpm,
        description,
        channel,
        bath=bath,
        bath_temperature_file=bath_temperature_file,
        accumet=accumet,
        rinse_data_file=rinse_data_file,
        log_file=log_file,
    )


def run_reglo_rinse_step(reglo_pump, step, bath=None, bath_temperature_file=None, accumet=None, rinse_data_file=None, log_file=None):
    description = step["description"]
    channel = step["channel"]
    volume_ml = step["volume_ml"]

    if "flow_rate_ml_min" in step:
        dispense_reglo_volume_at_flow_rate(
            reglo_pump,
            volume_ml,
            step["flow_rate_ml_min"],
            description,
            channel,
            bath=bath,
            bath_temperature_file=bath_temperature_file,
            accumet=accumet,
            rinse_data_file=rinse_data_file,
            log_file=log_file,
        )
        return

    dispense_reglo_volume_at_rpm(
        reglo_pump,
        volume_ml,
        step["rpm"],
        description,
        channel,
        bath=bath,
        bath_temperature_file=bath_temperature_file,
        accumet=accumet,
        rinse_data_file=rinse_data_file,
        log_file=log_file,
    )


def run_post_run_reglo_flush(skip_ports=None, bath=None, bath_temperature_file=None, accumet=None, rinse_data_file=None, log_file=None):
    reglo_pump = find_reglo_icc_pump(skip_ports=skip_ports)
    try:
        if log_file is not None:
            log(log_file, f"{datetime.datetime.now()}, starting Masterflex Reglo ICC post-run flush...")
        if accumet is not None:
            accumet.reset_input_buffer()
        for step in REGLO_RINSE_STEPS:
            run_reglo_rinse_step(
                reglo_pump,
                step,
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
    images_folder = run_paths["images_folder"]
    bath_temperature_file = run_paths["bath_temperature_file"]
    rinse_data_file = run_paths["rinse_data_file"]
    rinse_bath_temperature_file = run_paths["rinse_bath_temperature_file"]
    initialize_data_file(output_file)
    initialize_bath_temperature_file(bath_temperature_file)
    initialize_rinse_data_file(rinse_data_file)
    initialize_rinse_bath_temperature_file(rinse_bath_temperature_file)

    print(f"Saving run data in {run_paths['run_folder']}")
    print(f"Saving run video to {video_file}")
    print(f"Saving first-two-hour camera images to {images_folder}")
    print(f"Saving bath temperature data to {bath_temperature_file}")
    print(f"Saving rinse data to {rinse_data_file}")
    print(f"Saving rinse bath temperature data to {rinse_bath_temperature_file}")

    grace_period = MINIMUM_TIME_MINUTES
    hard_stop = float(input("Maximum Time (min): "))
    hard_stop_seconds = hard_stop * 60
    MasterflexPump.format_time_seconds(hard_stop_seconds)

    run_reglo_flush_after_run = prompt_yes_no_default_yes(
        "Run the post-run flush after the run?"
    )
    turn_off_bath_after_run = prompt_yes_no_default_yes(
        "Turn off the heating bath after the run?"
    )

    accumet = None
    pump = None
    cap = None
    video = None
    bath = None
    main_pump_started = False

    try:
        accumet, pump = find_devices()
        bath = find_isotemp_bath(skip_ports=(accumet.port, pump.port))
        print(bath.set_unit_on(True))
        print("Fisher Isotemp bath turned on.")
        log(log_file, f"{datetime.datetime.now()}, Fisher Isotemp bath turned on.")
        accumet.set_csv_output()
        pump.enable_remote()
        pump.set_time_mode()
        pump.set_run_time_seconds(hard_stop_seconds)
        pump.set_speed_rpm(PUMP_SPEED_RPM)

        cap = cv2.VideoCapture(CAMERA_INDEX)
        if not cap.isOpened():
            raise RuntimeError("Could not open camera.")

        print("Camera started.")

        pump.require_ack(pump.start())
        main_pump_started = True
        run_start_time = time.monotonic()
        accumet.reset_input_buffer()

        min_time = datetime.datetime.now() + datetime.timedelta(minutes=grace_period)
        max_time = datetime.datetime.now() + datetime.timedelta(minutes=hard_stop)
        print(f"Will start checking conductivity until {min_time.strftime(RUN_TIME_DISPLAY_FORMAT)}")
        print(f"Hard stop at {max_time.strftime(RUN_TIME_DISPLAY_FORMAT)}")

        log(log_file, f"Starting run at {datetime.datetime.now()}...")
        log(log_file, f"Resin: {resin_name}")

        next_frame_time = run_start_time
        next_image_time = run_start_time
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
            monotonic_time = time.monotonic()
            elapsed_seconds = monotonic_time - run_start_time
            video_frame_due = monotonic_time >= next_frame_time
            image_due = (
                elapsed_seconds < IMAGE_CAPTURE_DURATION_SECONDS
                and monotonic_time >= next_image_time
            )
            if video_frame_due or image_due:
                ret, frame = cap.read()
                if ret:
                    frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)

                    if video_frame_due:
                        if video is None:
                            height, width, _channels = frame.shape
                            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                            video = cv2.VideoWriter(video_file, fourcc, VIDEO_FPS, (width, height))
                            if not video.isOpened():
                                raise RuntimeError(f"Could not open video file for writing: {video_file}")

                        video.write(frame)
                        print(f"Appended frame to video: {video_file}")
                        while next_frame_time <= monotonic_time:
                            next_frame_time += FRAME_INTERVAL_SECONDS

                    if image_due:
                        elapsed_minutes = elapsed_seconds / 60
                        image_filename = f"image_{elapsed_minutes:.2f}_minutes.jpg"
                        image_file = os.path.join(images_folder, image_filename)
                        if not cv2.imwrite(image_file, frame):
                            print(f"Warning: could not save camera image: {image_file}")
                        else:
                            print(f"Saved camera image: {image_file}")
                            while next_image_time <= monotonic_time:
                                next_image_time += IMAGE_INTERVAL_SECONDS
                else:
                    print("Warning: Camera failed to grab frame.")

            if current_time - last_bath_temperature_time >= BATH_TEMPERATURE_INTERVAL_SECONDS:
                try:
                    save_bath_temperature(bath, bath_temperature_file)
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

        if run_reglo_flush_after_run and main_pump_started:
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
        elif not run_reglo_flush_after_run:
            print("Masterflex Reglo ICC post-run flush skipped.")
            log(log_file, f"{datetime.datetime.now()}, skipped Masterflex Reglo ICC post-run flush.")
        else:
            print("Masterflex Reglo ICC post-run flush not started because the main run never started.")
            log(
                log_file,
                f"{datetime.datetime.now()}, did not start Masterflex Reglo ICC post-run flush "
                "because the main run never started.",
            )

        if accumet is not None:
            accumet.close()
            accumet = None

        if turn_off_bath_after_run:
            try:
                turn_off_isotemp_bath(bath)
                bath = None
            except (OSError, RuntimeError, serial.SerialException, UnicodeDecodeError, ValueError) as error:
                print(f"Warning: error while turning off Fisher Isotemp bath: {error}")
        else:
            if bath is not None:
                try:
                    bath.close()
                except (OSError, serial.SerialException) as error:
                    print(f"Warning: error while closing Fisher Isotemp bath connection: {error}")
                finally:
                    bath = None
            print("Fisher Isotemp heating bath left on.")
            log(log_file, f"{datetime.datetime.now()}, Fisher Isotemp heating bath left on.")

        print("Run complete.")


if __name__ == "__main__":
    main()
