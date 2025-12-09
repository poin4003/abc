# scan_phone_numbers.py
import re
import time
import serial
from serial.tools import list_ports


def send_at(ser: serial.Serial, cmd: str, wait: float = 0.8) -> str:
    """
    Gửi 1 lệnh AT và đọc response.
    """
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    full = cmd.strip() + "\r\n"
    ser.write(full.encode("utf-8"))
    time.sleep(wait)

    resp = ""
    while ser.in_waiting:
        line = ser.readline().decode(errors="ignore")
        resp += line
    return resp.strip()


def parse_cnum(resp: str):
    """
    Parse số điện thoại từ response AT+CNUM
    Ví dụ:
      +CNUM: "","+84901234567",129,7,4
    → lấy 84901234567
    """
    for line in resp.splitlines():
        if "+CNUM:" in line:
            # Dùng regex lấy chuỗi trong cặp " "
            m = re.search(r'\+CNUM:.*?"([^"]+)"', line)
            if m:
                number = m.group(1).strip()
                if number:
                    return number
    return None


def probe_port_for_number(port: str, baudrate: int = 115200, timeout: float = 1.0):
    info = {
        "port": port,
        "at_ok": False,
        "phone_number": None,
        "raw_cnum": None,
        "error": None,
    }

    try:
        # Windows: nếu COM > 9 thì dùng r"\\.\COM10"
        ser = serial.Serial(port=port, baudrate=baudrate, timeout=timeout)
    except Exception as e:
        info["error"] = f"open_error: {e!r}"
        return info

    try:
        # Test AT
        resp_at = send_at(ser, "AT")
        if "OK" not in resp_at:
            info["error"] = f"AT no OK, resp={resp_at!r}"
            ser.close()
            return info

        info["at_ok"] = True

        # Gửi AT+CNUM lấy số
        resp_cnum = send_at(ser, "AT+CNUM", wait=1.5)
        info["raw_cnum"] = resp_cnum

        number = parse_cnum(resp_cnum)
        if number:
            info["phone_number"] = number
        else:
            info["error"] = "CNUM empty or not supported"

        ser.close()
        return info

    except Exception as e:
        info["error"] = f"runtime_error: {e!r}"
        try:
            ser.close()
        except Exception:
            pass
        return info


def scan_all_com_ports():
    results = []
    ports = list_ports.comports()
    for p in ports:
        # p.device = "COM3", "COM7", "/dev/ttyUSB2", ...
        res = probe_port_for_number(p.device)
        res["description"] = p.description
        results.append(res)
    return results


if __name__ == "__main__":
    for item in scan_all_com_ports():
        print("-" * 40)
        print(f"Port:        {item['port']} ({item.get('description')})")
        print(f"AT OK:       {item['at_ok']}")
        print(f"Phone number:{item['phone_number']}")
        print(f"Raw CNUM:    {item['raw_cnum']}")
        print(f"Error:       {item['error']}")