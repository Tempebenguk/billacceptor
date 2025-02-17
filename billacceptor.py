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
TOLERANCE = 2  # Toleransi ±2 pulsa
MIN_PULSE_INTERVAL = 0.04  # Interval minimal antara pulsa

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

def log_transaction(message):
    timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    with open(LOG_FILE, "a") as log:
        log.write(f"{timestamp} {message}\n")
    print(f"{timestamp} {message}")

# 📌 Inisialisasi Flask
app = Flask(__name__)

# 📌 Variabel Global
pulse_count = 0
last_pulse_time = time.time()
transaction_active = False
remaining_balance = 0
id_trx = None
total_pulses = 0  # Total pulsa yang masuk
total_inserted = 0  # Total uang yang dimasukkan
waiting_pulses = False  # Menunggu pulsa selesai sebelum dikonversi

# 📌 Inisialisasi pigpio
pi = pigpio.pi()
if not pi.connected:
    log_transaction("⚠️ Gagal terhubung ke pigpio daemon!")
    exit()

pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 0)

def closest_valid_pulse(pulses):
    """Mengoreksi jumlah pulsa ke nilai terdekat yang valid."""
    if pulses == 1:
        return 1
    if 2 < pulses < 5:
        return 2
    closest_pulse = min(PULSE_MAPPING.keys(), key=lambda x: abs(x - pulses) if x != 1 else float("inf"))
    return closest_pulse if abs(closest_pulse - pulses) <= TOLERANCE else None

def process_pulse_addition():
    """Menangani akumulasi pulsa dan koreksi pulsa."""
    global total_pulses

    if total_pulses == 0:
        return  # Tidak ada pulsa baru, tidak perlu proses

    corrected_pulses = closest_valid_pulse(total_pulses)
    if corrected_pulses:
        print(f"\r🔄 Total pulsa terdeteksi: {total_pulses} -> Koreksi: {corrected_pulses}", end="")
        log_transaction(f"🔢 Pulsa terkoreksi: {corrected_pulses}")
    else:
        print("\r⚠️ Pulsa tidak valid!", end="")
        log_transaction("⚠️ Pulsa tidak valid, transaksi gagal.")
    
    return corrected_pulses

def convert_pulses_to_money(corrected_pulses):
    """Mengubah jumlah pulsa menjadi nominal uang."""
    global total_inserted

    if corrected_pulses is None:
        return 0  # Tidak ada pulsa valid

    received_amount = PULSE_MAPPING.get(corrected_pulses, 0)
    total_inserted += received_amount
    print(f"\r💰 Konversi pulsa ke uang: Rp.{received_amount}", end="")
    log_transaction(f"💰 Uang masuk: Rp.{received_amount}, Total: Rp.{total_inserted}")
    
    return received_amount

def deduct_from_balance(received_amount):
    """Mengurangi tagihan dari total uang yang masuk."""
    global remaining_balance, transaction_active, waiting_pulses, id_trx

    # 🕒 Tambahkan delay sebelum saldo dikurangi
    time.sleep(1)  # Delay 1 detik sebelum mengurangi tagihan

    remaining_balance -= received_amount
    print(f"\r💳 Saldo setelah pembayaran: Rp.{remaining_balance}", end="")

    if remaining_balance <= 0:
        overpaid_amount = abs(remaining_balance)  # Kelebihan bayar
        remaining_balance = 0
        transaction_active = False
        waiting_pulses = False
        pi.write(EN_PIN, 0)  # Matikan bill acceptor
        print(f"\r✅ Transaksi selesai! Kelebihan bayar: Rp.{overpaid_amount}", end="")
        log_transaction(f"✅ Transaksi {id_trx} selesai. Kelebihan: Rp.{overpaid_amount}")

        # Kirim API bahwa transaksi selesai
        try:
            print("📡 Mengirim status transaksi ke server...")
            response = requests.post("http://172.16.100.160:5000/api/receive",
                                     json={"id_trx": id_trx, "status": "success", "total_inserted": total_inserted, "overpaid": overpaid_amount},
                                     timeout=5)
            print(f"✅ POST sukses: {response.status_code}, Response: {response.text}")
            log_transaction(f"📡 Data transaksi dikirim ke server. Status: {response.status_code}, Response: {response.text}")
        except requests.exceptions.RequestException as e:
            log_transaction(f"⚠️ Gagal mengirim status transaksi: {e}")
            print(f"⚠️ Gagal mengirim status transaksi: {e}")
    else:
        print(f"\r💳 Saldo sisa: Rp.{remaining_balance}, Menunggu uang tambahan...", end="")
        log_transaction(f"💳 Saldo sisa: Rp.{remaining_balance}. Menunggu uang tambahan.")
        waiting_pulses = True  # Masih menunggu pulsa baru

def count_pulse(gpio, level, tick):
    """Handler untuk mendeteksi pulsa dari bill acceptor."""
    global total_pulses, last_pulse_time, waiting_pulses

    if not transaction_active:
        return

    current_time = time.time()

    # Pastikan debounce
    if (current_time - last_pulse_time) > DEBOUNCE_TIME:
        total_pulses += 1
        last_pulse_time = current_time
        print(f"🔢 Pulsa diterima: {total_pulses}")  # Debugging untuk melihat pulsa

    waiting_pulses = True

    # Tunggu pulsa selesai sebelum diproses
    time.sleep(PULSE_TIMEOUT)
    corrected_pulses = process_pulse_addition()
    received_amount = convert_pulses_to_money(corrected_pulses)
    deduct_from_balance(received_amount)

# Endpoint untuk memulai transaksi
@app.route("/api/ba", methods=["POST"])
def trigger_transaction():
    global transaction_active, remaining_balance, id_trx, total_inserted, total_pulses, waiting_pulses

    if transaction_active:
        return jsonify({"status": "error", "message": "Transaksi sedang berlangsung"}), 400
    
    data = request.json
    remaining_balance = int(data.get("total", 0))
    id_trx = data.get("id_trx")
    
    if remaining_balance <= 0 or id_trx is None:
        return jsonify({"status": "error", "message": "Data tidak valid"}), 400
    
    transaction_active = True
    waiting_pulses = True
    total_inserted = 0
    total_pulses = 0
    log_transaction(f"🔔 Transaksi dimulai! ID: {id_trx}, Tagihan: Rp.{remaining_balance}")
    
    pi.write(EN_PIN, 1)
    return jsonify({"status": "success", "message": "Transaksi dimulai"})

if __name__ == "__main__":
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)
    app.run(host="0.0.0.0", port=5000, debug=True)
