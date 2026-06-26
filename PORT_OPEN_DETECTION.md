# Serial Port Detection

This program avoids opening a Linux serial port twice by checking whether another process
already has the port device open before calling `serial.Serial(...)`.

## Where It Happens

The check is implemented in `port_is_open(port)` in `deashing_helpers.py`.

Every serial connection goes through `open_serial_port(port, **serial_kwargs)`, which calls
`port_is_open(port)` first. If the port is already open, the program raises an error instead of
opening it again:

```python
def open_serial_port(port, **serial_kwargs):
    if port_is_open(port):
        raise RuntimeError(f"Serial port is already open: {port}")

    return serial.Serial(port=port, exclusive=True, **serial_kwargs)
```

## How The Check Works

On Linux, every running process has a directory under `/proc`:

```text
/proc/<pid>/fd/
```

That `fd` directory contains symbolic links for every file descriptor the process currently has
open. Serial ports such as `/dev/ttyUSB0` and `/dev/ttyACM0` are device files, so if a process has
one open, one of its file descriptors points to that device.

The program:

1. Resolves the real device path with `os.path.realpath(port)`.
2. Loops through numeric process IDs in `/proc`.
3. Looks inside each `/proc/<pid>/fd` directory.
4. Resolves each file descriptor target with `os.path.realpath(fd_path)`.
5. Returns `True` if any open file descriptor points to the same serial device.

If nothing points to the device, it returns `False`.

## Why `realpath` Matters

Linux device paths may involve symlinks. For example, a stable device name might eventually point
to `/dev/ttyUSB1`. Using `os.path.realpath(...)` on both the requested port and each open file
descriptor lets the program compare the actual underlying device instead of only comparing names.

## What Happens During Auto-Detection

When the program searches for USB devices, it calls `port_is_open(port)` before probing each port.
If a port is already open, it prints:

```text
Skipping /dev/ttyUSB0: already open.
```

Then it moves on to the next USB serial port.

## Second Safeguard

After the pre-check passes, PySerial is opened with:

```python
exclusive=True
```


