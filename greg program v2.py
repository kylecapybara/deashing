import datetime
import os
import time

import serial

from deashing_helpers import (
    MasterflexPump,
    create_run_paths,
    find_devices,
    log,
)


COND_LIMIT_US_CM = 35
STOP_LIMIT = 50  # Consecutive measurements above the threshold.
MINIMUM_TIME_MINUTES = 180

CAMERA_INDEX = 0
IMAGE_INTERVAL_SECONDS = 60

IMAGE_FILENAME_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"
RUN_TIME_DISPLAY_FORMAT = '%Y-%m-%d %H:%M:%S'


def main():
    import cv2

    run_paths = create_run_paths()
    output_file = run_paths["data_file"]
    log_file = run_paths["log_file"]
    output_folder = run_paths["image_folder"]

    print(f"Saving run data in {run_paths['run_folder']}")

    grace_period = MINIMUM_TIME_MINUTES
    hard_stop = float(input("Maximum Time (min): "))

    accumet = None
    pump = None
    cap = None

    try:
        accumet, pump = find_devices()
        accumet.set_csv_output()
        pump.enable_remote()
        pump.set_speed()

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

        last_image_time = 0
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
            if current_time - last_image_time >= IMAGE_INTERVAL_SECONDS:
                ret, frame = cap.read()
                if ret:
                    timestamp = time.strftime(IMAGE_FILENAME_TIMESTAMP_FORMAT)
                    filename = os.path.join(output_folder, f"image_{timestamp}.jpg")
                    cv2.imwrite(filename, frame)
                    print(f"Saved: {filename}")
                    last_image_time = current_time
                else:
                    print("Warning: Camera failed to grab frame.")

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

        if accumet is not None:
            accumet.close()

        if cap is not None:
            cap.release()
            cv2.destroyAllWindows()
            print("Camera released. Run complete.")


if __name__ == "__main__":
    main()
