# sms_one_com.py
import serial
import threading
import time
import re

# 🔁 Đổi lại cho đúng port
PORT = "COM7"          # "COM7" trên Windows, "/dev/ttyUSB2" trên Linux
BAUDRATE = 115200
TIMEOUT = 1.0


def send_at(ser: serial.Serial, cmd: str, wait: float = 0.5) -> str:
    """
    Gửi 1 lệnh AT, đợi một chút rồi đọc response.
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


def init_modem_for_sms(ser: serial.Serial):
    """
    Cấu hình modem để nhận / gửi SMS ở TEXT mode
    và đẩy SMS mới lên ngay (không cần AT+CMGR).
    """
    print("[INIT] Test AT...")
    print(send_at(ser, "AT"))

    print("[INIT] Set text mode (AT+CMGF=1)...")
    print(send_at(ser, "AT+CMGF=1"))

    print('[INIT] Set charset GSM (AT+CSCS="GSM")...')
    print(send_at(ser, 'AT+CSCS="GSM"'))

    # CNMI=2,2,0,0,0 → incoming SMS sẽ được gửi lên ngay dạng +CMT: ...
    print("[INIT] Enable new SMS indication (AT+CNMI=2,2,0,0,0)...")
    print(send_at(ser, "AT+CNMI=2,2,0,0,0"))

    print("[INIT] Done. Ready for SMS.")


def parse_cmt_header(line: str):
    """
    Parse header CMT: +CMT: "<sender>",...
    Trả về số điện thoại nếu có.
    """
    # Ví dụ: +CMT: "+84901234567","","24/01/01,12:34:56+28"
    m = re.search(r'\+CMT:\s*"([^"]+)"', line)
    if m:
        return m.group(1)
    return None


def sms_listener(ser: serial.Serial):
    """
    Thread lắng nghe SMS: khi có CMT mới thì in ra màn hình.
    """
    print("[LISTENER] Start listening for incoming SMS...")
    try:
        while True:
            line = ser.readline().decode(errors="ignore").strip()
            if not line:
                continue

#  có thể bật debug raw nếu muốn
#             # print("[RAW]", line)

            if line.startswith("+CMT:"):
                sender = parse_cmt_header(line)
                # Dòng tiếp theo thường là nội dung SMS
                text = ser.readline().decode(errors="ignore").strip()

                print("\n===== NEW SMS =====")
                print(f"From   : {sender}")
                print(f"Text   : {text}")
                print("===================\n")
    except Exception as e:
        print("[LISTENER] Error:", e)


def send_sms(ser: serial.Serial, phone: str, text: str, wait: float = 5.0):
    """
    Gửi 1 SMS tới số 'phone' với nội dung 'text'.
    """
    # Đảm bảo đang ở TEXT mode
    send_at(ser, "AT+CMGF=1")

    # Bước 1: báo sẽ gửi SMS cho số phone
    cmd = f'AT+CMGS="{phone}"'
    ser.write((cmd + "\r").encode("utf-8"))
    time.sleep(0.5)

    # Có thể đọc '>' prompt nếu muốn:
    # prompt = ser.read(ser.in_waiting or 1).decode(errors="ignore")
    # print("PROMPT:", repr(prompt))

    # Bước 2: gửi nội dung + Ctrl+Z (ASCII 26)
    ser.write((text + "\x1A").encode("utf-8"))

    # Chờ modem xử lý
    time.sleep(wait)

    resp = ""
    while ser.in_waiting:
        line = ser.readline().decode(errors="ignore")
        resp += line
    print("[SEND_SMS] Response:\n", resp.strip())


def main():
    try:
        # Mở 1 lần và giữ suốt
        ser = serial.Serial(PORT, BAUDRATE, timeout=TIMEOUT)
        print(f"[MAIN] Opened {PORT} OK")

        # Init modem cho SMS
        init_modem_for_sms(ser)

        # Start listener thread
        t = threading.Thread(target=sms_listener, args=(ser,), daemon=True)
        t.start()

        # Vòng lặp cho phép gửi SMS bằng tay
        while True:
            print("\n--- SEND SMS MENU ---")
            phone = input("Nhập số điện thoại (Enter để bỏ qua, 'exit' để thoát): ").strip()
            if phone.lower() == "exit":
                break
            if not phone:
                # Không gửi, chỉ tiếp tục listen
                continue

            text = input("Nhập nội dung SMS: ").strip()
            if not text:
                print("⚠️ Nội dung rỗng, bỏ qua.")
                continue

            send_sms(ser, phone, text)

        print("[MAIN] Closing port...")
        ser.close()

    except Exception as e:
        print(f"[MAIN] Error opening {PORT}:", e)


if __name__ == "__main__":
    main()