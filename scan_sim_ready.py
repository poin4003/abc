# scan_sim_ready.py
import serial
from serial.tools import list_ports
from time import sleep


def send_at(ser, cmd: str, wait: float = 0.5) -> str:
    ser.write((cmd.strip() + "\r\n").encode())
    sleep(wait)
    data = ""
    while ser.in_waiting:
        data += ser.readline().decode(errors="ignore")
    return data.strip()


def probe_sim_on_port(port: str, baudrate: int = 115200, timeout: float = 1.0):
    try:
        ser = serial.Serial(port=port, baudrate=baudrate, timeout=timeout)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    result = {"port": port}

    try:
        # test AT
        resp_at = send_at(ser, "AT")
        result["at_ok"] = "OK" in resp_at
        result["at_raw"] = resp_at

        if not result["at_ok"]:
            ser.close()
            return result

        # check sim
        resp_cpin = send_at(ser, "AT+CPIN?")
        result["cpin_raw"] = resp_cpin

        if "READY" in resp_cpin:
            result["sim_status"] = "READY"
        elif "SIM PIN" in resp_cpin:
            result["sim_status"] = "NEED_PIN"
        elif "SIM PUK" in resp_cpin:
            result["sim_status"] = "NEED_PUK"
        elif "SIM NOT INSERTED" in resp_cpin:
            result["sim_status"] = "NOT_INSERTED"
        else:
            result["sim_status"] = "UNKNOWN"

        ser.close()
        return result
    except Exception as e:
        ser.close()
        return {"ok": False, "error": str(e)}


def scan_all_sim_ports():
    ports = list_ports.comports()
    results = []
    for p in ports:
        results.append(probe_sim_on_port(p.device))
    return results


if __name__ == "__main__":
    for item in scan_all_sim_ports():
        print(item)