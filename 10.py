#!/usr/bin/env python3
import serial
import sys

# Force UTF-8 so redirect works on Windows
sys.stdout.reconfigure(encoding='utf-8')

ser = serial.Serial("COM11", 115200, timeout=0.1)
print("Reading raw bytes from HLK-LD6002B...\n")
while True:
    data = ser.read(256)
    if data:
        print(data.hex(), flush=True)
