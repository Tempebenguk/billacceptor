import pigpio
import time
import datetime
import os
import requests
from flask import Flask, request, jsonify
import threading

# 📌 Konfigurasi PIN GPIO
BILL_ACCEPTOR_PIN = 14  # Pin pulsa dari bill acceptor (DT)
EN_PIN = 15             # Pin enable untuk mengaktifkan bill acceptor

# 📌 Konfigurasi transaksi
TIMEOUT = 15  # Waktu maksimum transaksi sebelum cooldown (detik)
DEBOUNCE_TIME = 0.05  # 50ms debounce
TOLERANCE = 2  # Toleransi ±2 pulsa
MAX_RETRIES = 3  # Maksimum percobaan pengiriman data ke server
RETRY_DELAY = 5  # Jeda antar retry (detik)

# 📌 Mapping jumlah pulsa ke nominal uang
PULSE_MAPPING = {
    1: 1000,
    2: 2000,
    5: 5000,
    10: 10000,
    20: 20000,
    50: 50000,
    100: 100000
}

# 📌 Kode Respon ISO 8583
ISO_CODES = {
    "success": "00",   # Transaksi sukses
    "failed": "05",    # Transaksi gagal
    "insufficient": "51",  # Saldo tidak cukup
    "overpaid": "07"   # Kelebihan pembayaran
}

# 📌 Lokasi penyimpanan log transaksi
LOG_DIR = "./logs"
LOG_FILE = os.path.join(LOG_DIR, "log.txt")

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

def log_transaction(message):
    timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    with open(LOG_FILE, "a") as log:
        log.write(f"{timestamp} {message}\n")
    print(f"{timestamp} {message}")

# 📌 Inisialisasi Flask
app = Flask(__name__)

# 📌 Variabel Global
pulse_count = 0
transaction_active = False
remaining_balance = 0
id_trx = None
total_inserted = 0

# 📌 Inisialisasi pigpio
pi = pigpio.pi()
if not pi.connected:
    log_transaction("⚠️ Gagal terhubung ke pigpio daemon!")
    exit()

# Atur EN_PIN ke 0 saat awal
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 0)  # Mematikan bill acceptor saat awal

# Fungsi untuk mengirim status transaksi dengan mekanisme retry
def send_transaction_status(status, total_inserted, overpaid, remaining_due):
    url = "http://172.16.100.165:5000/api/receive"
    payload = {
        "id_trx": id_trx,
        "status": status,
        "iso_code": ISO_CODES[status],
        "total_inserted": total_inserted,
        "overpaid": overpaid,
        "remaining_due": remaining_due
    }
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(url, json=payload, timeout=5)
            log_transaction(f"📡 POST sukses: {response.status_code}, Response: {response.text}")
            return
        except requests.exceptions.RequestException as e:
            log_transaction(f"⚠️ Gagal mengirim data (percobaan {attempt}): {e}")
            time.sleep(RETRY_DELAY)
    
    log_transaction("❌ Gagal mengirim status transaksi setelah beberapa percobaan")

# API untuk Memulai Transaksi
@app.route("/api/ba", methods=["POST"])
def trigger_transaction():
    global transaction_active, remaining_balance, id_trx, total_inserted

    if transaction_active:
        return jsonify({"status": "error", "message": "Transaksi sedang berlangsung"}), 400
    
    data = request.json
    remaining_balance = int(data.get("total", 0))
    id_trx = data.get("id_trx")
    
    if remaining_balance <= 0 or id_trx is None:
        return jsonify({"status": "error", "message": "Data tidak valid"}), 400
    
    transaction_active = True
    total_inserted = 0
    
    log_transaction(f"🔔 Transaksi dimulai! ID: {id_trx}, Tagihan: Rp.{remaining_balance}")

    return jsonify({"status": "success", "message": "Transaksi dimulai"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
