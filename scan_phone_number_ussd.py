# scan_phone_numbers_ussd.py
import re
import time
import serial
from serial.tools import list_ports


# TODO: chỉnh lại cho đúng nhà mạng của bạn
# Ví dụ: Viettel có thể là "*0#" hoặc "*888#" (bạn test thực tế rồi sửa)
USSD_CODES = [
    "*0#",      # ví dụ: kiểm tra số thuê bao
    "*888#",    # ví dụ: kênh tổng đài riêng
    "*101#",    # thường là tài khoản, đôi khi hiển thị cả số
]


def send_at(ser: serial.Serial, cmd: str, wait: float = 0.5) -> str:
    """
    Gửi 1 lệnh AT và đọc response ngay sau đó.
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


def send_ussd_and_wait(ser: serial.Serial, ussd: str, wait_total: float = 10.0) -> str:
    """
    Gửi USSD và chờ nhận +CUSD trong khoảng wait_total giây.
    USSD được gửi dạng: AT+CUSD=1,"*xxx#",15
    """
    # set charset cho chắc
    send_at(ser, 'AT+CSCS="GSM"')
    # bật CUSD + gửi code
    cmd = f'AT+CUSD=1,"{ussd}",15'
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    ser.write((cmd + "\r\n").encode("utf-8"))

    start = time.time()
    buf = ""

    # đọc liên tục đến khi hết thời gian
    while time.time() - start < wait_total:
        if ser.in_waiting:
            chunk = ser.read(ser.in_waiting).decode(errors="ignore")
            buf += chunk
        time.sleep(0.3)

    return buf.strip()


def extract_msisdn_from_text(text: str):
    """
    Tìm số điện thoại trong text USSD:
    - Dạng 84xxxxxxxxx hoặc 0xxxxxxxxx (9-11 digits).
    Lấy candidate đầu tiên.
    """
    # loại bỏ khoảng trắng unicode
    clean = text.replace("\u200b", "").replace("\xa0", " ")
    # tìm 84xxxxxxxxx
    candidates = re.findall(r'(84\d{8,10}|0\d{8,10})', clean)
    if candidates:
        return candidates[0]
    return None


def probe_port_for_number_with_ussd(
    port: str,
    baudrate: int = 115200,
    timeout: float = 1.0,
    ussd_codes=None,
):
    if ussd_codes is None:
        ussd_codes = USSD_CODES

    info = {
        "port": port,
        "at_ok": False,
        "sim_ready": False,
        "phone_number": None,
        "raw_ussd": None,
        "used_ussd": None,
        "error": None,
    }

    try:
        ser = serial.Serial(port=port, baudrate=baudrate, timeout=timeout)
    except Exception as e:
        info["error"] = f"open_error: {e!r}"
        return info

    try:
        # 1) Test AT
        resp_at = send_at(ser, "AT")
        if "OK" not in resp_at:
            info["error"] = f"AT no OK, resp={resp_at!r}"
            ser.close()
            return info

        info["at_ok"] = True

        # 2) Check SIM ready
        resp_cpin = send_at(ser, "AT+CPIN?")
        if "READY" not in resp_cpin:
            info["error"] = f"SIM not READY, CPIN={resp_cpin!r}"
            ser.close()
            return info

        info["sim_ready"] = True

        # 3) Thử từng USSD code
        for code in ussd_codes:
            ussd_resp = send_ussd_and_wait(ser, code, wait_total=12.0)
            if not ussd_resp:
                continue

            msisdn = extract_msisdn_from_text(ussd_resp)
            if msisdn:
                info["phone_number"] = msisdn
                info["raw_ussd"] = ussd_resp
                info["used_ussd"] = code
                break
            else:
                # vẫn log raw để debug
                info["raw_ussd"] = ussd_resp

        if not info["phone_number"]:
            info["error"] = "Cannot extract phone from USSD response"

        ser.close()
        return info

    except Exception as e:
        info["error"] = f"runtime_error: {e!r}"
        try:
            ser.close()
        except Exception:
            pass
        return info


def scan_all_com_ports_with_ussd():
    results = []
    ports = list_ports.comports()
    for p in ports:
        res = probe_port_for_number_with_ussd(p.device)
        res["description"] = p.description
        results.append(res)
    return results


if __name__ == "__main__":
    for item in scan_all_com_ports_with_ussd():
        print("-" * 50)
        print(f"Port:        {item['port']} ({item.get('description')})")
        print(f"AT OK:       {item['at_ok']}")
        print(f"SIM READY:   {item['sim_ready']}")
        print(f"Phone number:{item['phone_number']}")
        print(f"USSD used:   {item['used_ussd']}")
        print(f"Raw USSD:    {item['raw_ussd']}")
        print(f"Error:       {item['error']}")