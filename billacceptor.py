import pigpio
import time
import datetime
import os
import requests
from flask import Flask, request, jsonify

# 📌 Konfigurasi PIN GPIO
BILL_ACCEPTOR_PIN = 14  # Pin DT
EN_PIN = 15             # Pin EN

# 📌 Konfigurasi transaksi
TIMEOUT = 15  # Waktu maksimum transaksi sebelum cooldown (detik)
PULSE_TIMEOUT = 0.3  # Batas waktu antara pulsa untuk menentukan akhir transaksi (detik)
DEBOUNCE_TIME = 0.05  # 50ms debounce
TOLERANCE = 2  # Toleransi ±2 pulsa

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

# 📌 Lokasi penyimpanan log
LOG_DIR = "/var/www/html/logs"
LOG_FILE = os.path.join(LOG_DIR, "log.txt")

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

# 📌 Fungsi Logging
def log_transaction(message):
    with open(LOG_FILE, "a") as log:
        timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
        log.write(f"{timestamp} {message}\n")
    print(message)

# 📌 Inisialisasi Flask
app = Flask(__name__)

# 📌 Variabel Global
pulse_count = 0
last_pulse_time = time.time()
transaction_active = False
remaining_balance = 0
id_trx = None
cooldown_start = None

# 📌 Inisialisasi pigpio
pi = pigpio.pi()
if not pi.connected:
    log_transaction("⚠️ Gagal terhubung ke pigpio daemon!")
    exit()

pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 0)

# 📌 Koreksi jumlah pulsa
def closest_valid_pulse(pulses):
    if pulses == 1:
        return 1
    if 2 < pulses < 5:
        return 2
    closest_pulse = min(PULSE_MAPPING.keys(), key=lambda x: abs(x - pulses) if x != 1 else float("inf"))
    return closest_pulse if abs(closest_pulse - pulses) <= TOLERANCE else None

# 📌 Callback pulsa
def count_pulse(gpio, level, tick):
    global pulse_count, last_pulse_time, transaction_active, remaining_balance, cooldown_start
    current_time = time.time()
    if transaction_active:
        if (current_time - last_pulse_time) > DEBOUNCE_TIME:
            pulse_count += 1
            last_pulse_time = current_time
            cooldown_start = time.time()
            pi.write(EN_PIN, 1)

pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)

# 📌 API Endpoint untuk menerima trigger transaksi
@app.route("/api/ba", methods=["POST"])
def trigger_transaction():
    global transaction_active, remaining_balance, id_trx, cooldown_start

    if transaction_active:
        return jsonify({"status": "error", "message": "Transaksi sedang berlangsung"}), 400
    
    data = request.json
    remaining_balance = data.get("total", 0)
    id_trx = data.get("id_trx")
    
    if remaining_balance <= 0 or id_trx is None:
        return jsonify({"status": "error", "message": "Data tidak valid"}), 400
    
    transaction_active = True
    cooldown_start = time.time()
    log_transaction(f"🔔 Transaksi dimulai! ID: {id_trx}, Tagihan: Rp.{remaining_balance}")
    
    pi.write(EN_PIN, 1)
    return jsonify({"status": "success", "message": "Transaksi dimulai"})

# 📌 API Endpoint untuk mengirimkan hasil transaksi
@app.route("/api/feedback", methods=["POST"])
def send_feedback():
    global transaction_active, pulse_count, remaining_balance, id_trx, cooldown_start
    
    if not transaction_active:
        return jsonify({"status": "idle", "message": "Tidak ada transaksi aktif"})
    
    current_time = time.time()
    while transaction_active:
        if pulse_count > 0 and (current_time - last_pulse_time > PULSE_TIMEOUT):
            received_pulses = pulse_count
            pulse_count = 0
            corrected_pulses = closest_valid_pulse(received_pulses)
            if corrected_pulses:
                received_amount = PULSE_MAPPING[corrected_pulses]
                remaining_balance -= received_amount
                log_transaction(f"💰 Uang masuk: Rp.{received_amount} (Sisa: Rp.{remaining_balance})")
                pi.write(EN_PIN, 1)
            
            if remaining_balance <= 0:
                log_transaction(f"✅ Pembayaran transaksi {id_trx} selesai.")
                transaction_active = False
                pi.write(EN_PIN, 0)
                requests.post("http://172.16.100.160:5000/api/receive", json={"id_trx": id_trx, "status": "success", "remaining": 0})
                return jsonify({"status": "success", "message": "Pembayaran selesai"})
        
        if (current_time - cooldown_start) > TIMEOUT:
            log_transaction("⚠️ Timeout! Menutup transaksi.")
            pi.write(EN_PIN, 0)
            transaction_active = False
            requests.post("http://172.16.100.160:5000/api/receive", json={"id_trx": id_trx, "status": "pending", "remaining": remaining_balance})
            return jsonify({"status": "error", "message": "Timeout"})
        
        time.sleep(0.1)
    
    return jsonify({"status": "error", "message": "Terjadi kesalahan"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)