# test_one_com_ussd.py
import time
import serial
import re

PORT = "COM7"      # 🔁 chỉnh lại port bạn muốn test
BAUDRATE = 115200
TIMEOUT = 1.0
USSD_CODE = "*0#"  # 🔁 đổi theo mã kiểm tra số thuê bao / tài khoản của nhà mạng


def send_at(ser: serial.Serial, cmd: str, wait: float = 0.8) -> str:
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    ser.write((cmd.strip() + "\r\n").encode("utf-8"))
    time.sleep(wait)

    resp = ""
    while ser.in_waiting:
        line = ser.readline().decode(errors="ignore")
        resp += line
    return resp.strip()


def send_ussd_and_wait(ser: serial.Serial, ussd: str, wait_total: float = 10.0) -> str:
    # set charset
    send_at(ser, 'AT+CSCS="GSM"')

    cmd = f'AT+CUSD=1,"{ussd}",15'
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    ser.write((cmd + "\r\n").encode("utf-8"))

    start = time.time()
    buf = ""

    while time.time() - start < wait_total:
        if ser.in_waiting:
            chunk = ser.read(ser.in_waiting).decode(errors="ignore")
            buf += chunk
        time.sleep(0.3)

    return buf.strip()


def extract_msisdn(text: str):
    clean = text.replace("\u200b", "").replace("\xa0", " ")
    candidates = re.findall(r'(84\d{8,10}|0\d{8,10})', clean)
    return candidates[0] if candidates else None


def main():
    try:
        with serial.Serial(PORT, BAUDRATE, timeout=TIMEOUT) as ser:
            print(f"Opened {PORT} OK")

            # AT
            resp_at = send_at(ser, "AT")
            print("AT RESP:\n", resp_at)
            if "OK" not in resp_at:
                print("❌ Modem không trả OK cho AT")
                return

            # CPIN
            resp_cpin = send_at(ser, "AT+CPIN?")
            print("CPIN RESP:\n", resp_cpin)
            if "READY" not in resp_cpin:
                print("❌ SIM chưa READY (PIN/PUK hoặc chưa gắn SIM)")
                return

            # USSD
            print(f"Gửi USSD: {USSD_CODE}")
            ussd_resp = send_ussd_and_wait(ser, USSD_CODE, wait_total=12.0)
            print("USSD RESP:\n", ussd_resp)

            msisdn = extract_msisdn(ussd_resp)
            if msisdn:
                print("✅ Phone number:", msisdn)
            else:
                print("⚠️ Không extract được số từ USSD (regex chưa match / nội dung không chứa số)")

    except Exception as e:
        print(f"❌ Error opening/writing {PORT}: {e!r}")


if __name__ == "__main__":
    main()