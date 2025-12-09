import tkinter as tk
from tkinter import ttk, messagebox
import serial
from serial.tools import list_ports
import threading
import time
import re
import queue


# ==========================
#  Helper AT / SMS / USSD
# ==========================

def parse_cmt_header(line: str):
    """
    Parse header +CMT: "<sender>",...
    Trả về số gửi nếu có.
    """
    m = re.search(r'\+CMT:\s*"([^"]+)"', line)
    if m:
        return m.group(1)
    return None


class ModemSession:
    """
    Quản lý 1 modem trên 1 COM:
    - serial instance
    - thread listener đọc SMS & log
    - queue để gửi event lên GUI
    """
    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1.0, event_queue: queue.Queue = None):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser: serial.Serial | None = None
        self.running = False
        self.thread: threading.Thread | None = None
        self.event_queue = event_queue or queue.Queue()

    def open(self):
        if self.ser and self.ser.is_open:
            return

        self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)

        # Init modem SMS trước khi start listener
        self._init_modem_for_sms()

        self.running = True
        self.thread = threading.Thread(target=self._listener_loop, daemon=True)
        self.thread.start()

        self._push_log(f"[SYSTEM] Connected to {self.port}")

    def close(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            # Không cần join lâu – thread là daemon
            time.sleep(0.2)
        if self.ser and self.ser.is_open:
            self.ser.close()
            self._push_log(f"[SYSTEM] Disconnected from {self.port}")

    # ---------- low-level ----------

    def _send_at(self, cmd: str, wait: float = 0.5) -> str:
        """
        Chỉ dùng trong init trước khi listener chạy,
        vì có thao tác read blocking.
        """
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Serial not open")

        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        full = cmd.strip() + "\r\n"
        self.ser.write(full.encode("utf-8"))
        time.sleep(wait)

        resp = ""
        while self.ser.in_waiting:
            line = self.ser.readline().decode(errors="ignore")
            resp += line
        return resp.strip()

    def _init_modem_for_sms(self):
        """
        Cấu hình modem để:
        - SMS text mode
        - charset GSM
        - tự đẩy SMS mới lên (CNMI=2,2,...)
        """
        try:
            at_resp = self._send_at("AT")
            self._push_log(f"[INIT] AT → {at_resp!r}")

            cmgf_resp = self._send_at("AT+CMGF=1")
            self._push_log(f"[INIT] AT+CMGF=1 → {cmgf_resp!r}")

            cscs_resp = self._send_at('AT+CSCS="GSM"')
            self._push_log(f"[INIT] AT+CSCS=\"GSM\" → {cscs_resp!r}")

            cnmi_resp = self._send_at("AT+CNMI=2,2,0,0,0")
            self._push_log(f"[INIT] AT+CNMI=2,2,0,0,0 → {cnmi_resp!r}")

        except Exception as e:
            self._push_log(f"[ERROR] Init modem failed: {e!r}")
            raise

    def _listener_loop(self):
        """
        Đọc liên tục từ serial:
        - Nếu +CMT: → đọc thêm 1 dòng nội dung → báo event 'sms'
        - Còn lại → báo event 'log'
        """
        self._push_log("[LISTENER] Start listening for incoming data...")
        try:
            while self.running and self.ser and self.ser.is_open:
                try:
                    line_bytes = self.ser.readline()
                    if not line_bytes:
                        continue
                    line = line_bytes.decode(errors="ignore").strip()
                    if not line:
                        continue

                    # SMS incoming
                    if line.startswith("+CMT:"):
                        sender = parse_cmt_header(line)
                        # dòng tiếp theo là nội dung SMS
                        text_line = self.ser.readline().decode(errors="ignore").strip()

                        self._push_sms(sender, text_line)
                    else:
                        # log tất cả dòng khác để xem USSD / debug
                        self._push_log(line)

                except Exception as e:
                    self._push_log(f"[LISTENER ERROR] {e!r}")
                    time.sleep(0.5)
        finally:
            self._push_log("[LISTENER] Stopped.")

    def _push_log(self, text: str):
        if self.event_queue:
            self.event_queue.put(("log", text))

    def _push_sms(self, sender: str | None, text: str):
        if self.event_queue:
            self.event_queue.put(("sms", sender, text))

    # ---------- public actions ----------

    def send_sms(self, phone: str, text: str):
        """
        Gửi SMS: chỉ write, đọc response sẽ do listener log lên.
        """
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Modem not connected")

        # đảm bảo text mode
        self.ser.write(b"AT+CMGF=1\r")
        time.sleep(0.3)

        cmd = f'AT+CMGS="{phone}"'
        self.ser.write((cmd + "\r").encode("utf-8"))
        time.sleep(0.5)

        # gửi nội dung + Ctrl+Z
        self.ser.write((text + "\x1A").encode("utf-8"))
        self._push_log(f"[SEND_SMS] To {phone}: {text}")

    def send_ussd(self, ussd_code: str):
        """
        Gửi USSD: listener sẽ nhận +CUSD: ... rồi đẩy lên log.
        """
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Modem not connected")

        # set charset & bật CUSD
        self.ser.write(b'AT+CSCS="GSM"\r')
        time.sleep(0.2)
        cmd = f'AT+CUSD=1,"{ussd_code}",15'
        self.ser.write((cmd + "\r").encode("utf-8"))

        self._push_log(f"[USSD] Sent {ussd_code}")


# ==========================
#  GUI APP
# ==========================

class SimGuiApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SIM Manager - COM / SMS / USSD")
        self.geometry("900x600")

        self.session: ModemSession | None = None
        self.event_queue: queue.Queue = queue.Queue()

        self._build_widgets()

        # Poll queue mỗi 200ms để update UI
        self.after(200, self._poll_events)

    def _build_widgets(self):
        # ---- top frame: COM control ----
        top_frame = ttk.Frame(self)
        top_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=5)

        ttk.Label(top_frame, text="COM Port:").pack(side=tk.LEFT)

        self.combobox_port = ttk.Combobox(top_frame, width=20, state="readonly")
        self.combobox_port.pack(side=tk.LEFT, padx=5)

        btn_refresh = ttk.Button(top_frame, text="Refresh", command=self.refresh_ports)
        btn_refresh.pack(side=tk.LEFT, padx=5)

        self.btn_connect = ttk.Button(top_frame, text="Connect", command=self.toggle_connect)
        self.btn_connect.pack(side=tk.LEFT, padx=5)

        # ---- middle: log / sms ----
        middle_frame = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        middle_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # Log frame
        log_frame = ttk.Labelframe(middle_frame, text="Log / USSD / System")
        self.text_log = tk.Text(log_frame, wrap="word", state="disabled")
        scrollbar_log = ttk.Scrollbar(log_frame, command=self.text_log.yview)
        self.text_log.configure(yscrollcommand=scrollbar_log.set)
        self.text_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar_log.pack(side=tk.RIGHT, fill=tk.Y)
        middle_frame.add(log_frame, weight=2)

        # SMS frame
        sms_frame = ttk.Labelframe(middle_frame, text="Incoming SMS")
        self.text_sms = tk.Text(sms_frame, wrap="word", state="disabled")
        scrollbar_sms = ttk.Scrollbar(sms_frame, command=self.text_sms.yview)
        self.text_sms.configure(yscrollcommand=scrollbar_sms.set)
        self.text_sms.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar_sms.pack(side=tk.RIGHT, fill=tk.Y)
        middle_frame.add(sms_frame, weight=1)

        # ---- bottom: actions (SMS / USSD) ----
        bottom_frame = ttk.Frame(self)
        bottom_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=5)

        # send SMS panel
        sms_panel = ttk.Labelframe(bottom_frame, text="Send SMS")
        sms_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)

        ttk.Label(sms_panel, text="Phone:").grid(row=0, column=0, sticky="w")
        self.entry_phone = ttk.Entry(sms_panel, width=20)
        self.entry_phone.grid(row=0, column=1, sticky="w", padx=5, pady=2)

        ttk.Label(sms_panel, text="Text:").grid(row=1, column=0, sticky="nw")
        self.text_sms_out = tk.Text(sms_panel, width=40, height=3)
        self.text_sms_out.grid(row=1, column=1, sticky="we", padx=5, pady=2)

        btn_send_sms = ttk.Button(sms_panel, text="Send SMS", command=self.on_send_sms)
        btn_send_sms.grid(row=2, column=1, sticky="e", padx=5, pady=5)

        # send USSD panel
        ussd_panel = ttk.Labelframe(bottom_frame, text="Send USSD")
        ussd_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)

        ttk.Label(ussd_panel, text="USSD code:").grid(row=0, column=0, sticky="w")
        self.entry_ussd = ttk.Entry(ussd_panel, width=20)
        self.entry_ussd.grid(row=0, column=1, sticky="w", padx=5, pady=2)

        btn_send_ussd = ttk.Button(ussd_panel, text="Send USSD", command=self.on_send_ussd)
        btn_send_ussd.grid(row=1, column=1, sticky="e", padx=5, pady=5)

        self.refresh_ports()

    # ---------- UI helpers ----------

    def refresh_ports(self):
        ports = list_ports.comports()
        values = [p.device for p in ports]
        self.combobox_port["values"] = values
        if values:
            self.combobox_port.current(0)

    def toggle_connect(self):
        if self.session is None:
            # connect
            port = self.combobox_port.get()
            if not port:
                messagebox.showwarning("Warning", "No COM port selected")
                return
            try:
                self.session = ModemSession(port=port, event_queue=self.event_queue)
                self.session.open()
                self.btn_connect.config(text="Disconnect")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to connect {port}:\n{e}")
                self.session = None
        else:
            # disconnect
            try:
                self.session.close()
            except Exception:
                pass
            self.session = None
            self.btn_connect.config(text="Connect")

    def append_log(self, text: str):
        self.text_log.config(state="normal")
        self.text_log.insert("end", text + "\n")
        self.text_log.see("end")
        self.text_log.config(state="disabled")

    def append_sms(self, sender: str | None, text: str):
        self.text_sms.config(state="normal")
        header = f"From: {sender or 'Unknown'}\n"
        body = f"{text}\n"
        line = f"{'-' * 30}\n{header}{body}"
        self.text_sms.insert("end", line)
        self.text_sms.see("end")
        self.text_sms.config(state="disabled")

    def _poll_events(self):
        """
        Lấy event từ queue (do thread listener push lên) và update UI.
        """
        try:
            while True:
                event = self.event_queue.get_nowait()
                if not event:
                    break

                etype = event[0]
                if etype == "log":
                    _, text = event
                    self.append_log(text)
                elif etype == "sms":
                    _, sender, text = event
                    self.append_sms(sender, text)

        except queue.Empty:
            pass

        # lặp lại
        self.after(200, self._poll_events)

    # ---------- callbacks ----------

    def on_send_sms(self):
        if not self.session:
            messagebox.showwarning("Warning", "No modem connected")
            return

        phone = self.entry_phone.get().strip()
        text = self.text_sms_out.get("1.0", "end").strip()

        if not phone or not text:
            messagebox.showwarning("Warning", "Phone and text are required")
            return

        try:
            self.session.send_sms(phone, text)
            self.append_log(f"[UI] Sent SMS to {phone}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to send SMS:\n{e}")

    def on_send_ussd(self):
        if not self.session:
            messagebox.showwarning("Warning", "No modem connected")
            return

        code = self.entry_ussd.get().strip()
        if not code:
            messagebox.showwarning("Warning", "USSD code is required")
            return

        try:
            self.session.send_ussd(code)
            self.append_log(f"[UI] Sent USSD {code}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to send USSD:\n{e}")


def main():
    app = SimGuiApp()
    app.mainloop()


if __name__ == "__main__":
    main()
