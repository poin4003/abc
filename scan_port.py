# scan_ports.py
from serial.tools import list_ports

def list_serial_ports():
    ports = list_ports.comports()
    results = []
    for p in ports:
        results.append({
            "device": p.device,        # /dev/ttyUSB0, COM3, ...
            "name": p.name,
            "description": p.description,
            "hwid": p.hwid,
            "vid": hex(p.vid) if p.vid else None,
            "pid": hex(p.pid) if p.pid else None,
        })
    return results


if __name__ == "__main__":
    for info in list_serial_ports():
        print(info)