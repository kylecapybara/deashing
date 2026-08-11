import serial
import datetime
import time
import cv2
import os  

def log(message):
    '''log a message to log file. automatically adds linebreak.'''
    with open(log_file,'a') as f:
        f.write(message + "\n")


#accumet_port = input("Accumet COM Port: ")
accumet_port = '/dev/ttyUSB1'
#pump_port = input("Pump COM Port: ")
pump_port = '/dev/ttyUSB2'

output_file_input = input("Output file name (no extention!): ")
date_suffix = datetime.datetime.now().strftime("%Y%m%d")
output_file = f"{output_file_input}_{date_suffix}"
log_file = output_file + "_LOG"
# make a directory for image capture and saving
output_folder = output_file + '_captured_images'
os.makedirs(output_folder, exist_ok=True)


accumet = serial.Serial(port=accumet_port,timeout=30)
# (kyle on feb 24) changed timeout to 30s  to see if that is issue.
# also made log file to check issues
pump = serial.Serial(port=pump_port,baudrate=115200,timeout=5)

pump.write("@1\r".encode('ascii'))
pump.readline()
pump.write("1RE1\r".encode('ascii'))
pump.readline()
pump.write("1R579\r".encode('ascii'))
pump.readline()

running = True
cond_limit = 35
grace_period = float(input("Minimum Time (min): "))
hard_stop = float(input("Maximum Time (min): "))
stop_count = 0
stop_lim = 50 # 50 CONSECUTIVE measurements above the threshold. -kw
speed_rpm = 5.79
time_format = '%Y-%m-%d %H:%M:%S'


# Initialize camera
cap = cv2.VideoCapture(0)

if not cap.isOpened():
    print("Error: Could not open camera.")
    exit()

print("Camera started.")


pump.write("1H\r".encode('ascii'))
accumet.reset_input_buffer()

min_time = datetime.datetime.now() + datetime.timedelta(minutes=grace_period)
max_time = datetime.datetime.now() + datetime.timedelta(minutes=hard_stop)
print(f"Will start checking conductivity until {min_time.strftime(time_format)}")
print(f"Hard stop at {max_time.strftime(time_format)}")


log(f"Starting run at {datetime.datetime.now()}...")

# Track the last time an image was taken. 
# Initializing to 0 ensures an image is captured immediately on the first loop.
last_image_time = 0
image_interval = 60

while running:
    # Read serial line
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

    # IMAGE CAPTURE BLOCK
    # Check if 60 seconds have passed since the last photo
    current_time = time.time()
    if current_time - last_image_time >= image_interval:
        ret, frame = cap.read()
        if ret:
            # Generate a unique filename using a timestamp
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            filename = os.path.join(output_folder, f"image_{timestamp}.jpg")
            
            # Save the image
            cv2.imwrite(filename, frame)
            print(f"Saved: {filename}")
            
            # Update the last capture timestamp
            last_image_time = current_time
        else:
            print("Warning: Camera failed to grab frame.")

    # Check for hard stop time
    if datetime.datetime.now() > max_time:
        print("Reached hard stop")
        running = False
        # Fallback date/hour if serial read hasn't populated them yet
        try:
            log(f"{date}{hour}, reached end time limit (hard stop)...")
        except NameError:
            log(f"{datetime.datetime.now()}, reached end time limit (hard stop)...")

# Clean up pump
time.sleep(1)
pump.write("1I\r".encode('ascii'))
print(pump.readline())
time.sleep(1)
pump.write("1RE0\r".encode('ascii'))
print(pump.readline())
time.sleep(1)

# CLEAN UP CAMERA (Crucial to prevent memory leaks or system locking the camera)
cap.release()
cv2.destroyAllWindows()
print("Camera released. Run complete.")
