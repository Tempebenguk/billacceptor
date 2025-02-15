import pigpio
import time
import datetime
import os
import requests
from flask import Flask, request, jsonify

# 📌 Konfigurasi PIN GPIO
BILL_ACCEPTOR_PIN = 14  # Pin pulsa dari bill acceptor (DT)
EN_PIN = 15             # Pin enable untuk mengaktifkan bill acceptor

# 📌 Konfigurasi transaksi
TIMEOUT = 15  # Waktu maksimum transaksi sebelum cooldown (detik)
PULSE_TIMEOUT = 0.3  # Batas waktu antara pulsa untuk menentukan akhir transaksi (detik)
DEBOUNCE_TIME = 0.05  # 50ms debounce
MIN_PULSE_INTERVAL = 0.04  # 40ms minimum interval
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
pi.write(EN_PIN, 1)  # Standby mode (bill acceptor tertutup)

# 📌 Koreksi jumlah pulsa
def closest_valid_pulse(pulses):
    if pulses == 1:
        return 1  # Rp. 1000 harus pas
    if 2 < pulses < 5:
        return 2  # Koreksi ke 2 jika antara 3-4 pulsa
    closest_pulse = min(PULSE_MAPPING.keys(), key=lambda x: abs(x - pulses) if x != 1 else float("inf"))
    return closest_pulse if abs(closest_pulse - pulses) <= TOLERANCE else None

# 📌 Callback pulsa
def count_pulse(gpio, level, tick):
    global pulse_count, last_pulse_time, transaction_active, remaining_balance, cooldown_start
    current_time = time.time()

    if transaction_active and (current_time - last_pulse_time) > MIN_PULSE_INTERVAL:
        pulse_count += 1
        last_pulse_time = current_time
        cooldown_start = time.time()  # Reset cooldown setiap ada uang masuk
        pi.write(EN_PIN, 0)  # Biarkan bill acceptor tetap terbuka

pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)

# 📌 API Endpoint untuk menerima trigger transaksi
@app.route("/api/ba", methods=["POST"])
def trigger_transaction():
    global transaction_active, remaining_balance, id_trx, cooldown_start
    data = request.json
    total = data.get("total", 0)
    id_trx = data.get("id_trx")

    if total <= 0 or id_trx is None:
        return jsonify({"status": "error", "message": "Data tidak valid"}), 400

    remaining_balance = total
    transaction_active = True
    cooldown_start = time.time()
    
    log_transaction(f"🔔 Transaksi dimulai! ID: {id_trx}, Tagihan: Rp.{remaining_balance}")
    pi.write(EN_PIN, 0)  # Buka bill acceptor (aktif)
    return jsonify({"status": "success", "message": "Bill acceptor aktif"})

# 📌 Fungsi utama untuk mengecek status transaksi
def process_transaction():
    global transaction_active, pulse_count, remaining_balance, id_trx, cooldown_start

    while transaction_active:
        current_time = time.time()

        # Jika ada pulsa masuk
        if pulse_count > 0 and (current_time - last_pulse_time > PULSE_TIMEOUT):
            received_pulses = pulse_count
            pulse_count = 0
            corrected_pulses = closest_valid_pulse(received_pulses)

            if corrected_pulses:
                received_amount = PULSE_MAPPING[corrected_pulses]
                remaining_balance -= received_amount
                log_transaction(f"💰 Uang masuk: Rp.{received_amount} (Sisa: Rp.{remaining_balance})")
                pi.write(EN_PIN, 0)  # Biarkan bill acceptor tetap terbuka

            # Jika pembayaran selesai
            if remaining_balance <= 0:
                log_transaction(f"✅ Pembayaran transaksi {id_trx} selesai.")
                transaction_active = False
                pi.write(EN_PIN, 1)  # Tutup bill acceptor (standby)
                requests.post("http://172.16.100.160:5000/api/receive", json={
                    "id_trx": id_trx,
                    "status": "success",
                    "remaining": 0
                })
                return

        # Timeout transaksi
        if (current_time - cooldown_start) > TIMEOUT:
            log_transaction("⚠️ Timeout! Menutup transaksi.")
            pi.write(EN_PIN, 1)  # Tutup bill acceptor (standby)
            transaction_active = False
            requests.post("http://172.16.100.160:5000/api/receive", json={
                "id_trx": id_trx,
                "status": "pending",
                "remaining": remaining_balance
            })
            return

        time.sleep(0.1)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)