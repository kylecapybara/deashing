import serial
import datetime
import time

#accumet_port = input("Accumet COM Port: ")
accumet_port = '/dev/ttyUSB0'
#pump_port = input("Pump COM Port: ")
pump_port = '/dev/ttyUSB1'

output_file = input("Output file name (no extention!): ")
log_file = output_file + "_LOG"

accumet = serial.Serial(port=accumet_port,timeout=1)
accumet = serial.Serial(port=accumet_port,timeout=300)
# (kyle on feb 24) changed timeout to 300s = 5 mins to see if that is issue.
# also made log file to check issues
pump = serial.Serial(port=pump_port,baudrate=115200,timeout=5)

pump.write("@1\r".encode('ascii'))
pump.readline()
pump.write("1RE1\r".encode('ascii'))
pump.readline()
pump.write("1R579\r".encode('ascii'))
pump.readline()

running = True
cond_limit = float(input("Conductivity limit (uS/cm): "))
grace_period = float(input("Minimum Time (min): "))
hard_stop = float(input("Maximum Time (min): "))
stop_count = 0
stop_lim = 50
speed_rpm = 5.79
time_format = '%Y-%m-%d %H:%M:%S'

pump.write("1H\r".encode('ascii'))
accumet.reset_input_buffer()

min_time = datetime.datetime.now() + datetime.timedelta(minutes=grace_period)
max_time = datetime.datetime.now() + datetime.timedelta(minutes=hard_stop)
print(f"Will start checking conductivity until {min_time.strftime(time_format)}")
print(f"Hard stop at {max_time.strftime(time_format)}")

def log(message):
    '''log a message to log file. automatically adds linebreak.'''
    with open(log_file,'a') as f:
        f.write(message + "\n")
    

log(f"Starting run at {datetime.datetime.now()}...")

while running:
    line = accumet.readline().decode("cp437").split(",")
    if len(line) == 24:
        try:
            date = line[5]
            hour = line[6]
            cond = float(line[8])
            temp = float(line[12])
            print(f"{date} {hour}: cond = {cond:.3f} uS/cm; temp = {temp:.2f} C")

            if cond > cond_limit:
                stop_count += 1
                log(f"{date}{hour}, exceeded conductivity limit {stop_count} time(s)")

            if (cond > cond_limit) & (datetime.datetime.now() > min_time):
                stop_count += 1
                if stop_count >= stop_lim:
                    running = False
                    print("Stopping pump")
                log(f"{date}{hour}, exceeded conductivity limit {stop_count} time(s)")
            else:
                stop_count = 0

            try:
                with open(output_file,'a') as f:
                    f.write(f"{date}{hour},{cond},{temp}\n")
                    
            except:
                print("Error saving data to file")
                log(f"{date}{hour}, error saving data to file...")
                

        except:
            print("Error reading data")
            log(f"{date}{hour}, error reading data from probe...")

    if datetime.datetime.now() > max_time:
        print("Reached hard stop")
        running = False
        log(f"{date}{hour}, reached end time limit (hard stop)...")

time.sleep(1)
pump.write("1I\r".encode('ascii'))
print(pump.readline())
time.sleep(1)
pump.write("1RE0\r".encode('ascii'))
print(pump.readline())
time.sleep(1)
