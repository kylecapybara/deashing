import time

import serial

from greg_program_v11 import find_reglo_icc_pump


CHANNEL = 1
SPEED_RPM = 100
RUN_SECONDS = 10


def main():
    pump = None
    started = False

    try:
        pump = find_reglo_icc_pump()
        print(f"Connected to Reglo ICC pump on {pump.port}.")

        pump.set_rpm_mode(channel=CHANNEL)
        pump.set_clockwise(channel=CHANNEL)
        pump.set_speed_rpm(SPEED_RPM, channel=CHANNEL)

        print(
            f"Starting channel {CHANNEL} clockwise at {SPEED_RPM} RPM "
            f"for {RUN_SECONDS} seconds."
        )
        pump.start(channel=CHANNEL)
        started = True
        time.sleep(RUN_SECONDS)
        print("Pump test completed.")
    finally:
        if pump is not None:
            if started:
                try:
                    pump.stop(channel=CHANNEL)
                    print(f"Stopped channel {CHANNEL}.")
                except (OSError, RuntimeError, serial.SerialException) as error:
                    print(f"Warning: could not stop channel {CHANNEL}: {error}")

            pump.close()
            print("Serial connection closed.")


if __name__ == "__main__":
    main()
