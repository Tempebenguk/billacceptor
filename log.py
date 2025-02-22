from flask import Flask, request, jsonify
import time
import threading
import pigpio

app = Flask(__name__)

# Konstanta dan variabel global
EN_PIN = 17  # GPIO pin untuk bill acceptor
transaction_active = False
last_pulse_received_time = None
pi = pigpio.pi()

def log_transaction(message):
    """Cetak ke terminal untuk debugging"""
    print(f"[LOG] {time.strftime('%Y-%m-%d %H:%M:%S')} - {message}")

def fetch_invoice_details(payment_token):
    """Simulasi fetch data invoice dari API"""
    log_transaction(f"🔎 Mengambil detail invoice untuk token: {payment_token}")
    
    # Simulasi response API
    if payment_token == "valid_token":
        return "trx123", payment_token, 5000  # Contoh ID transaksi, token, dan harga
    return None, None, None

def start_timeout_timer():
    """Timer untuk memantau transaksi"""
    global transaction_active
    log_transaction("⏳ Timeout timer dimulai")
    time.sleep(10)  # Simulasi timeout 10 detik

    if transaction_active:
        log_transaction("⚠️ Timeout terjadi, membatalkan transaksi!")
        transaction_active = False
        pi.write(EN_PIN, 0)  # Matikan bill acceptor

@app.route("/api/ba", methods=["POST"])
def trigger_transaction():
    global transaction_active, last_pulse_received_time

    # Debug: print request data
    print(f"🔔 Request diterima: {request.data.decode('utf-8')}")  

    # Jika transaksi sedang berjalan, tolak request baru
    if transaction_active:
        log_transaction("⚠️ Transaksi sedang berlangsung!")
        response = jsonify({"status": "error", "message": "Transaksi sedang berlangsung"})
        print(f"🛑 Response dikirim: {response.get_json()}")
        return response, 400

    # Parse JSON dari request
    try:
        data = request.get_json()
        print(f"📥 Data JSON: {data}")
    except Exception as e:
        log_transaction(f"❌ Gagal membaca JSON: {e}")
        return jsonify({"status": "error", "message": "Invalid JSON format"}), 400

    if not data or "paymentToken" not in data:
        log_transaction("⚠️ Token pembayaran tidak ditemukan!")
        response = jsonify({"status": "error", "message": "Token pembayaran tidak valid"})
        print(f"🛑 Response dikirim: {response.get_json()}")
        return response, 400

    # Ambil token pembayaran
    payment_token = data.get("paymentToken")
    print(f"🔑 Token pembayaran: {payment_token}")

    # Ambil detail invoice dari API
    id_trx, payment_token, product_price = fetch_invoice_details(payment_token)

    if id_trx is None or product_price is None:
        log_transaction("❌ Invoice tidak valid atau sudah dibayar")
        response = jsonify({"status": "error", "message": "Invoice tidak valid atau sudah dibayar"})
        print(f"🛑 Response dikirim: {response.get_json()}")
        return response, 400

    # Set transaksi aktif dan mulai timer
    transaction_active = True
    last_pulse_received_time = time.time()  
    log_transaction(f"🔔 Transaksi dimulai! ID: {id_trx}, Token: {payment_token}, Tagihan: Rp.{product_price}")

    # Aktifkan bill acceptor
    pi.write(EN_PIN, 1)

    # Jalankan timeout di thread terpisah
    threading.Thread(target=start_timeout_timer, daemon=True).start()

    # Kirim response sukses
    response = jsonify({"status": "success", "message": "Transaksi dimulai"})
    print(f"🛑 Response dikirim: {response.get_json()}")  
    return response

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
