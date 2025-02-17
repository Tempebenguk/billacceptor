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
total_inserted = 0  # Total uang yang dimasukkan
last_pulse_received_time = time.time()  # Waktu terakhir pulsa diterima
PULSE_WAIT_TIME = 3  # Waktu tunggu setelah pulsa terakhir (detik)

# 📌 Inisialisasi pigpio
pi = pigpio.pi()
if not pi.connected:
    log_transaction("⚠️ Gagal terhubung ke pigpio daemon!")
    exit()

pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 0)

# Fungsi untuk menghitung pulsa dan memperbarui transaksi
def count_pulse(gpio, level, tick):
    global pulse_count, last_pulse_time, transaction_active, total_inserted, remaining_balance, id_trx, last_pulse_received_time

    if not transaction_active:
        return

    current_time = time.time()

    # Pastikan debounce
    if (current_time - last_pulse_time) > DEBOUNCE_TIME:
        pulse_count += 1
        last_pulse_time = current_time
        print(f"🔢 Pulsa diterima: {pulse_count}")  # Debugging untuk melihat pulsa

        # Konversi pulsa ke uang
        corrected_pulses = closest_valid_pulse(pulse_count)
        if corrected_pulses:
            received_amount = PULSE_MAPPING.get(corrected_pulses, 0)
            total_inserted += received_amount
            print(f"\r🔄 Perhitungan pulsa: {pulse_count} pulsa dikonversi menjadi Rp.{received_amount}", end="")  # Debugging
            print(f"\r💰 Total uang masuk: Rp.{total_inserted}", end="")
            log_transaction(f"💰 Total uang masuk: Rp.{total_inserted}")
            pulse_count = 0  # Reset pulse count setelah konversi

        # Cek apakah pulsa sudah cukup dan ada penundaan
        if total_inserted >= remaining_balance:
            # Jika tagihan sudah tercapai, tunggu beberapa detik untuk melihat apakah ada pulsa lain
            last_pulse_received_time = current_time  # Update waktu pulsa terakhir diterima
            print("\r⏳ Menunggu pulsa lebih lanjut sebelum menghitung...", end="")

    # Periksa apakah sudah cukup waktu sejak pulsa terakhir diterima
    if current_time - last_pulse_received_time > PULSE_WAIT_TIME:
        # Jika sudah cukup waktu, dan total_inserted sudah cukup untuk menyelesaikan transaksi
        if total_inserted >= remaining_balance:
            overpaid_amount = total_inserted - remaining_balance  # Kelebihan bayar jika total_inserted > remaining_balance
            remaining_balance = 0  # Set saldo menjadi 0 setelah transaksi selesai
            transaction_active = False  # Tandai transaksi selesai
            pi.write(EN_PIN, 0)  # Matikan bill acceptor
            print(f"\r✅ Transaksi selesai! Kelebihan bayar: Rp.{overpaid_amount}", end="")
            log_transaction(f"✅ Transaksi {id_trx} selesai. Kelebihan: Rp.{overpaid_amount}")

            # Kirim API bahwa transaksi sudah selesai
            try:
                print("📡 Mengirim status transaksi ke server...")
                response = requests.post("http://172.16.100.174:5000/api/receive",
                                         json={"id_trx": id_trx, "status": "success", "total_inserted": total_inserted, "overpaid": overpaid_amount},
                                         timeout=5)
                print(f"✅ POST sukses: {response.status_code}, Response: {response.text}")
                log_transaction(f"📡 Data pulsa dikirim ke server. Status: {response.status_code}, Response: {response.text}")
            except requests.exceptions.RequestException as e:
                log_transaction(f"⚠️ Gagal mengirim status transaksi: {e}")
                print(f"⚠️ Gagal mengirim status transaksi: {e}")
        else:
            print(f"\r💳 Saldo masih kurang, menunggu pulsa berikutnya...", end="")

# Fungsi untuk mendapatkan pulsa yang valid
def closest_valid_pulse(pulses):
    if pulses == 1:
        return 1
    if 2 < pulses < 5:
        return 2
    closest_pulse = min(PULSE_MAPPING.keys(), key=lambda x: abs(x - pulses) if x != 1 else float("inf"))
    return closest_pulse if abs(closest_pulse - pulses) <= TOLERANCE else None

# Endpoint untuk memulai transaksi
@app.route("/api/ba", methods=["POST"])
def trigger_transaction():
    global transaction_active, remaining_balance, id_trx, total_inserted

    if transaction_active:
        return jsonify({"status": "error", "message": "Transaksi sedang berlangsung"}), 400
    
    data = request.json
    remaining_balance = int(data.get("total", 0))  # Pastikan remaining_balance berupa integer
    id_trx = data.get("id_trx")
    
    if remaining_balance <= 0 or id_trx is None:
        return jsonify({"status": "error", "message": "Data tidak valid"}), 400
    
    transaction_active = True
    total_inserted = 0  # Reset total uang yang masuk untuk transaksi baru
    log_transaction(f"🔔 Transaksi dimulai! ID: {id_trx}, Tagihan: Rp.{remaining_balance}")
    print(f"Bill acceptor diaktifkan. Tagihan: Rp.{remaining_balance}")
    
    pi.write(EN_PIN, 1)
    return jsonify({"status": "success", "message": "Transaksi dimulai"})

if __name__ == "__main__":
    # Pasang callback untuk pin BILL_ACCEPTOR_PIN
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)
    app.run(host="0.0.0.0", port=5000, debug=True)
