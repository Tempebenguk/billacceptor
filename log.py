from flask import Flask, request, jsonify
import datetime

app = Flask(__name__)

# 📌 Lokasi penyimpanan log
LOG_FILE = "troubleshoot_log.txt"

def log_message(message):
    """Menyimpan log ke file dan mencetaknya ke terminal."""
    timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    log_entry = f"{timestamp} {message}\n"
    
    # Simpan ke file log
    with open(LOG_FILE, "a") as log:
        log.write(log_entry)
    
    # Cetak ke terminal langsung
    print(log_entry, end="", flush=True)

@app.route("/api/test", methods=["POST"])
def test_trigger():
    """Menerima request dan mencatatnya untuk troubleshooting."""
    data = request.json

    log_message(f"📥 Menerima request: {data}")

    # Cek apakah `paymentToken` ada di request
    payment_token = data.get("paymentToken")
    if not payment_token:
        log_message("⚠️ Token pembayaran tidak ditemukan!")
        return jsonify({"status": "error", "message": "Token pembayaran tidak valid"}), 400

    log_message(f"✅ Token valid: {payment_token}")
    
    return jsonify({"status": "success", "message": "Trigger diterima", "paymentToken": payment_token})

if __name__ == "__main__":
    print("\n🚀 API Troubleshooting berjalan di http://0.0.0.0:5000/api/test\n", flush=True)
    app.run(host="0.0.0.0", port=5000, debug=True)
