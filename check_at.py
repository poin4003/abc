import serial
from serial.tools import list_ports
from time import sleep

def probe_at_port(port: str, baudrate: int = 115200, timeout: float = 1.0):
    try:
        ser = serial.Serial(port=port, baudrate=baudrate, timeout=timeout)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    try:
        ser.write(b"AT\r\n")
        sleep(0.5)
        resp = ser.read(ser.in_waiting or 64).decode(errors="ignore")
        ser.close()
        return {
            "ok": "OK" in resp,
            "raw": resp.strip()
        }
    except Exception as e:
        ser.close()
        return {"ok": False, "error": str(e)}


def scan_modem_ports():
    ports = list_ports.comports()
    results = []
    for p in ports:
        info = {
            "device": p.device,
            "description": p.description,
        }
        probe = probe_at_port(p.device)
        info.update(probe)
        results.append(info)
    return results


if __name__ == "__main__":
    for item in scan_modem_ports():
        print(item)