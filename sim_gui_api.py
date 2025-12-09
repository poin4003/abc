import threading
import time
import re
import queue
from typing import Optional, Dict, List

import serial
from serial.tools import list_ports

# GUI
import tkinter as tk
from tkinter import ttk, messagebox

# API
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn


# ==========================
#  Modem session + manager
# ==========================

def parse_cmt_header(line: str) -> Optional[str]:
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
    1 modem trên 1 COM:
    - serial
    - listener thread lắng nghe SMS / log
    - event_queue: đẩy log/sms lên GUI
    - sms_store: lưu sms cho API đọc
    """
    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float = 1.0,
        event_queue: Optional[queue.Queue] = None,
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser: Optional[serial.Serial] = None
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.event_queue = event_queue
        self.lock = threading.Lock()

        # store sms cho API
        self.sms_store: List[Dict] = []
        self.sms_store_lock = threading.Lock()

    def _push_log(self, text: str):
        if self.event_queue:
            self.event_queue.put(("log", text))

    def _push_sms(self, sender: Optional[str], text: str):
        # đẩy lên GUI
        if self.event_queue:
            self.event_queue.put(("sms", sender, text))
        # lưu sms cho API
        with self.sms_store_lock:
            self.sms_store.append(
                {
                    "port": self.port,
                    "sender": sender,
                    "text": text,
                    "timestamp": time.time(),
                }
            )

    def _send_at_block(self, cmd: str, wait: float = 0.5) -> str:
        """
        Gửi AT trong giai đoạn init (trước khi listener loop chạy).
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
        try:
            at_resp = self._send_at_block("AT")
            self._push_log(f"[{self.port}] AT → {at_resp!r}")

            cmgf_resp = self._send_at_block("AT+CMGF=1")
            self._push_log(f"[{self.port}] AT+CMGF=1 → {cmgf_resp!r}")

            cscs_resp = self._send_at_block('AT+CSCS="GSM"')
            self._push_log(f"[{self.port}] AT+CSCS=\"GSM\" → {cscs_resp!r}")

            cnmi_resp = self._send_at_block("AT+CNMI=2,2,0,0,0")
            self._push_log(f"[{self.port}] AT+CNMI=2,2,0,0,0 → {cnmi_resp!r}")
        except Exception as e:
            self._push_log(f"[{self.port}] [ERROR] Init modem failed: {e!r}")
            raise

    def _listener_loop(self):
        self._push_log(f"[{self.port}] [LISTENER] Start listening...")
        try:
            while self.running and self.ser and self.ser.is_open:
                try:
                    line_bytes = self.ser.readline()
                    if not line_bytes:
                        continue
                    line = line_bytes.decode(errors="ignore").strip()
                    if not line:
                        continue

                    # Incoming SMS
                    if line.startswith("+CMT:"):
                        sender = parse_cmt_header(line)
                        # Dòng tiếp theo là nội dung SMS
                        text_line = self.ser.readline().decode(errors="ignore").strip()
                        self._push_sms(sender, text_line)
                    else:
                        # Log các dòng khác (USSD +CUSD, response AT, v.v.)
                        self._push_log(f"[{self.port}] {line}")
                except Exception as e:
                    self._push_log(f"[{self.port}] [LISTENER ERROR] {e!r}")
                    time.sleep(0.5)
        finally:
            self._push_log(f"[{self.port}] [LISTENER] Stopped.")

    def open(self):
        if self.ser and self.ser.is_open:
            return
        self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        self._init_modem_for_sms()
        self.running = True
        self.thread = threading.Thread(target=self._listener_loop, daemon=True)
        self.thread.start()
        self._push_log(f"[{self.port}] [SYSTEM] Connected")

    def close(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            time.sleep(0.2)
        if self.ser and self.ser.is_open:
            self.ser.close()
        self._push_log(f"[{self.port}] [SYSTEM] Disconnected")

    # -------- API actions (thread-safe write) --------

    def send_sms(self, phone: str, text: str):
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Modem not connected")

        with self.lock:
            # đảm bảo text mode
            self.ser.write(b"AT+CMGF=1\r")
            time.sleep(0.3)

            cmd = f'AT+CMGS="{phone}"'
            self.ser.write((cmd + "\r").encode("utf-8"))
            time.sleep(0.5)

            self.ser.write((text + "\x1A").encode("utf-8"))

        self._push_log(f"[{self.port}] [SEND_SMS] To {phone}: {text}")

    def send_ussd(self, ussd_code: str):
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("Modem not connected")

        with self.lock:
            self.ser.write(b'AT+CSCS="GSM"\r')
            time.sleep(0.2)
            cmd = f'AT+CUSD=1,"{ussd_code}",15'
            self.ser.write((cmd + "\r").encode("utf-8"))

        self._push_log(f"[{self.port}] [USSD] Sent {ussd_code}")


class SessionManager:
    """
    Quản lý nhiều ModemSession theo port.
    Dùng chung cho GUI & API.
    """
    def __init__(self, event_queue_for_gui: Optional[queue.Queue] = None):
        self.sessions: Dict[str, ModemSession] = {}
        self.lock = threading.Lock()
        self.gui_queue = event_queue_for_gui

    def list_sessions(self) -> List[str]:
        with self.lock:
            return list(self.sessions.keys())

    def get_session(self, port: str, create_if_missing: bool = False) -> Optional[ModemSession]:
        with self.lock:
            s = self.sessions.get(port)
            if s:
                return s
            if create_if_missing:
                s = ModemSession(port=port, event_queue=self.gui_queue)
                self.sessions[port] = s
                return s
        return None

    def connect(self, port: str) -> ModemSession:
        s = self.get_session(port, create_if_missing=True)
        s.open()
        return s

    def disconnect(self, port: str):
        with self.lock:
            s = self.sessions.get(port)
            if not s:
                return
            s.close()
            del self.sessions[port]


# global sẽ được gán sau khi GUI tạo event_queue
session_manager: Optional[SessionManager] = None


# ==========================
#  FastAPI definitions
# ==========================

api_app = FastAPI(title="SIM REST API", version="1.0.0")


class SmsRequest(BaseModel):
    phone: str
    text: str


class UssdRequest(BaseModel):
    code: str


@api_app.get("/ports")
def api_list_ports():
    ports = list_ports.comports()
    return [
        {
            "device": p.device,
            "description": p.description,
        }
        for p in ports
    ]


@api_app.get("/sessions")
def api_list_sessions():
    global session_manager
    if not session_manager:
        return []
    return session_manager.list_sessions()


@api_app.post("/sessions/{port}/connect")
def api_connect_port(port: str):
    global session_manager
    if not session_manager:
        raise HTTPException(status_code=500, detail="Session manager not ready")
    try:
        session_manager.connect(port)
        return {"status": "connected", "port": port}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api_app.post("/sessions/{port}/disconnect")
def api_disconnect_port(port: str):
    global session_manager
    if not session_manager:
        raise HTTPException(status_code=500, detail="Session manager not ready")
    session_manager.disconnect(port)
    return {"status": "disconnected", "port": port}


@api_app.post("/sessions/{port}/sms")
def api_send_sms(port: str, req: SmsRequest):
    global session_manager
    if not session_manager:
        raise HTTPException(status_code=500, detail="Session manager not ready")
    s = session_manager.get_session(port)
    if not s:
        raise HTTPException(status_code=404, detail="Session not connected")
    try:
        s.send_sms(req.phone, req.text)
        return {"status": "sent", "port": port, "phone": req.phone}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api_app.post("/sessions/{port}/ussd")
def api_send_ussd(port: str, req: UssdRequest):
    global session_manager
    if not session_manager:
        raise HTTPException(status_code=500, detail="Session manager not ready")
    s = session_manager.get_session(port)
    if not s:
        raise HTTPException(status_code=404, detail="Session not connected")
    try:
        s.send_ussd(req.code)
        return {"status": "sent", "port": port, "code": req.code}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api_app.get("/sessions/{port}/sms/inbox")
def api_sms_inbox(port: str):
    global session_manager
    if not session_manager:
        raise HTTPException(status_code=500, detail="Session manager not ready")
    s = session_manager.get_session(port)
    if not s:
        raise HTTPException(status_code=404, detail="Session not connected")
    with s.sms_store_lock:
        return s.sms_store


# ==========================
#  GUI APP
# ==========================

class SimGuiApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SIM Manager - GUI + REST API")
        self.geometry("900x600")

        self.event_queue: queue.Queue = queue.Queue()

        # gán session_manager global dùng chung GUI + API
        global session_manager
        session_manager = SessionManager(event_queue_for_gui=self.event_queue)

        self._build_widgets()

        # Poll queue mỗi 200ms để update UI
        self.after(200, self._poll_events)

    def _build_widgets(self):
        # top frame
        top_frame = ttk.Frame(self)
        top_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=5)

        ttk.Label(top_frame, text="COM Port:").pack(side=tk.LEFT)

        self.combobox_port = ttk.Combobox(top_frame, width=20, state="readonly")
        self.combobox_port.pack(side=tk.LEFT, padx=5)

        btn_refresh = ttk.Button(top_frame, text="Refresh", command=self.refresh_ports)
        btn_refresh.pack(side=tk.LEFT, padx=5)

        self.btn_connect = ttk.Button(top_frame, text="Connect", command=self.toggle_connect)
        self.btn_connect.pack(side=tk.LEFT, padx=5)

        # middle
        middle_frame = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        middle_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # log
        log_frame = ttk.Labelframe(middle_frame, text="Log / USSD / System")
        self.text_log = tk.Text(log_frame, wrap="word", state="disabled")
        scrollbar_log = ttk.Scrollbar(log_frame, command=self.text_log.yview)
        self.text_log.configure(yscrollcommand=scrollbar_log.set)
        self.text_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar_log.pack(side=tk.RIGHT, fill=tk.Y)
        middle_frame.add(log_frame, weight=2)

        # sms
        sms_frame = ttk.Labelframe(middle_frame, text="Incoming SMS")
        self.text_sms = tk.Text(sms_frame, wrap="word", state="disabled")
        scrollbar_sms = ttk.Scrollbar(sms_frame, command=self.text_sms.yview)
        self.text_sms.configure(yscrollcommand=scrollbar_sms.set)
        self.text_sms.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar_sms.pack(side=tk.RIGHT, fill=tk.Y)
        middle_frame.add(sms_frame, weight=1)

        # bottom
        bottom_frame = ttk.Frame(self)
        bottom_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=5)

        # send sms panel
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

        # ussd panel
        ussd_panel = ttk.Labelframe(bottom_frame, text="Send USSD")
        ussd_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)

        ttk.Label(ussd_panel, text="USSD code:").grid(row=0, column=0, sticky="w")
        self.entry_ussd = ttk.Entry(ussd_panel, width=20)
        self.entry_ussd.grid(row=0, column=1, sticky="w", padx=5, pady=2)

        btn_send_ussd = ttk.Button(ussd_panel, text="Send USSD", command=self.on_send_ussd)
        btn_send_ussd.grid(row=1, column=1, sticky="e", padx=5, pady=5)

        self.refresh_ports()

    def refresh_ports(self):
        ports = list_ports.comports()
        values = [p.device for p in ports]
        self.combobox_port["values"] = values
        if values:
            self.combobox_port.current(0)

    def toggle_connect(self):
        port = self.combobox_port.get()
        if not port:
            messagebox.showwarning("Warning", "No COM port selected")
            return

        global session_manager
        if not session_manager:
            messagebox.showerror("Error", "Session manager not ready")
            return

        # Nếu đang chưa connect -> connect
        sessions = session_manager.list_sessions()
        if port not in sessions:
            try:
                session_manager.connect(port)
                self.btn_connect.config(text="Disconnect")
                self.append_log(f"[GUI] Connected {port}")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to connect {port}:\n{e}")
        else:
            # đang connect -> disconnect
            try:
                session_manager.disconnect(port)
                self.btn_connect.config(text="Connect")
                self.append_log(f"[GUI] Disconnected {port}")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to disconnect {port}:\n{e}")

    def append_log(self, text: str):
        self.text_log.config(state="normal")
        self.text_log.insert("end", text + "\n")
        self.text_log.see("end")
        self.text_log.config(state="disabled")

    def append_sms(self, sender: Optional[str], text: str):
        self.text_sms.config(state="normal")
        header = f"From: {sender or 'Unknown'}\n"
        body = f"{text}\n"
        line = f"{'-' * 30}\n{header}{body}"
        self.text_sms.insert("end", line)
        self.text_sms.see("end")
        self.text_sms.config(state="disabled")

    def _poll_events(self):
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

        self.after(200, self._poll_events)

    def on_send_sms(self):
        port = self.combobox_port.get()
        if not port:
            messagebox.showwarning("Warning", "No COM port selected")
            return

        global session_manager
        if not session_manager:
            messagebox.showerror("Error", "Session manager not ready")
            return

        s = session_manager.get_session(port)
        if not s:
            messagebox.showwarning("Warning", f"Port {port} is not connected")
            return

        phone = self.entry_phone.get().strip()
        text = self.text_sms_out.get("1.0", "end").strip()

        if not phone or not text:
            messagebox.showwarning("Warning", "Phone and text are required")
            return

        try:
            s.send_sms(phone, text)
            self.append_log(f"[GUI] Sent SMS to {phone}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to send SMS:\n{e}")

    def on_send_ussd(self):
        port = self.combobox_port.get()
        if not port:
            messagebox.showwarning("Warning", "No COM port selected")
            return

        global session_manager
        if not session_manager:
            messagebox.showerror("Error", "Session manager not ready")
            return

        s = session_manager.get_session(port)
        if not s:
            messagebox.showwarning("Warning", f"Port {port} is not connected")
            return

        code = self.entry_ussd.get().strip()
        if not code:
            messagebox.showwarning("Warning", "USSD code is required")
            return

        try:
            s.send_ussd(code)
            self.append_log(f"[GUI] Sent USSD {code}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to send USSD:\n{e}")


# ==========================
#  Run API + GUI
# ==========================

def start_api():
    # Swagger: http://127.0.0.1:8000/docs
    uvicorn.run(api_app, host="127.0.0.1", port=8000, log_level="info")


def main():
    # start FastAPI in background thread
    api_thread = threading.Thread(target=start_api, daemon=True)
    api_thread.start()

    # start GUI
    app = SimGuiApp()
    app.mainloop()


if __name__ == "__main__":
    main()
