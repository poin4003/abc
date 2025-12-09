import json
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer

# Cấu hình
HOST = "localhost"
PORT = 8001  # Port của server tạm
API_MAIN_URL = "http://localhost:8000/api/v1/order/viettel_callback"  # URL callback tới API chính

class ViettelSendcharHandler(BaseHTTPRequestHandler):
    
    def _set_headers(self, status=200):
        """Thiết lập header cho response."""
        self.send_response(status)
        self.send_header("Content-type", "application/json")
        self.end_headers()

    def do_POST(self):
        """Xử lý POST request."""
        content_length = int(self.headers.get('Content-Length', 0))
        raw_body = self.rfile.read(content_length)

        try:
            payload = json.loads(raw_body)
            print("Received payload:", payload)  # Debug thông tin nhận được
        except json.JSONDecodeError:
            self._set_headers(400)
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            return

        if self.path == "/api/viettel_sendchar":
            # Nhận thông tin từ API chính và gửi lại kết quả
            print("Received data from send_to_viettel:", payload)

            # Lấy thông tin cần thiết từ payload
            order_id = payload.get("orderId")
            mobile = payload.get("mobile")
            
            # Kiểm tra nếu thiếu dữ liệu quan trọng
            if not order_id or not mobile:
                self._set_headers(400)
                self.wfile.write(json.dumps({"error": "Missing orderId or mobile"}).encode())
                return

            # Gửi callback vào API chính (chỉ gửi orderId và mobile)
            callback_payload = {
                "orderId": order_id,
                "mobile": mobile,
                "status": "SUCCESS",  # Giả định là thành công
                "reason": "Order processed successfully"  # Lý do giả định
            }

            try:
                # Gửi callback vào API chính
                callback_response = requests.post(API_MAIN_URL, json=callback_payload)
                callback_response.raise_for_status()  # Kiểm tra nếu có lỗi
                print("Callback successful:", callback_response.json())
            except Exception as e:
                print("Callback failed:", e)
                self._set_headers(500)
                self.wfile.write(json.dumps({"error": f"Callback failed: {str(e)}"}).encode())
                return

            # Trả về phản hồi thành công cho API chính
            self._set_headers(200)
            self.wfile.write(json.dumps({"message": "Viettel Sendchar received, callback sent"}).encode())

        else:
            self._set_headers(404)
            self.wfile.write(json.dumps({"error": "Not found"}).encode())

def run():
    """Khởi động server tạm trên địa chỉ và port được chỉ định."""
    server = HTTPServer((HOST, PORT), ViettelSendcharHandler)
    print(f"Temporary server running at http://{HOST}:{PORT}")
    server.serve_forever()

if __name__ == "__main__":
    run()
