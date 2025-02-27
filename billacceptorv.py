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

# 📌 Lokasi penyimpanan log transaksi
LOG_DIR = "/var/www/html/logs"
LOG_FILE = os.path.join(LOG_DIR, "log.txt")

# Buat direktori log jika belum ada
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

def log_transaction(message):
    """Menyimpan log transaksi ke file dan mencetak ke console."""
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
remaining_due = 0  # **Sisa tagihan jika uang kurang**
id_trx = None
total_inserted = 0  # Total uang yang dimasukkan
last_pulse_received_time = time.time()  # Waktu terakhir pulsa diterima

# 📌 Inisialisasi pigpio
pi = pigpio.pi()
if not pi.connected:
    log_transaction("⚠️ Gagal terhubung ke pigpio daemon!")
    exit()

# Set mode untuk pin GPIO
pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 0)  # Matikan bill acceptor saat awal

# Fungsi untuk mendapatkan pulsa yang valid
def closest_valid_pulse(pulses):
    """Mendapatkan jumlah pulsa yang paling mendekati nilai yang valid."""
    if pulses == 1:
        return 1
    if 2 < pulses < 5:
        return 2
    closest_pulse = min(PULSE_MAPPING.keys(), key=lambda x: abs(x - pulses) if x != 1 else float("inf"))
    return closest_pulse if abs(closest_pulse - pulses) <= TOLERANCE else None

# Fungsi untuk menghitung pulsa
def count_pulse(gpio, level, tick):
    """Menghitung pulsa dari bill acceptor dan mengonversinya ke nominal uang."""
    global pulse_count, last_pulse_time, transaction_active, total_inserted, remaining_balance, id_trx, last_pulse_received_time

    if not transaction_active:
        return

    current_time = time.time()

    # Pastikan debounce
    if (current_time - last_pulse_time) > DEBOUNCE_TIME:
        pulse_count += 1
        last_pulse_time = current_time
        last_pulse_received_time = current_time  # **Cooldown reset setiap pulsa masuk**
        print(f"🔢 Pulsa diterima: {pulse_count}")  # Debugging

        # Konversi pulsa ke uang
        corrected_pulses = closest_valid_pulse(pulse_count)
        if corrected_pulses:
            received_amount = PULSE_MAPPING.get(corrected_pulses, 0)
            total_inserted += received_amount
            print(f"\r💰 Total uang masuk: Rp.{total_inserted}", end="")
            log_transaction(f"💰 Total uang masuk: Rp.{total_inserted}")
            pulse_count = 0  # Reset pulse count setelah konversi

        # Update remaining_balance setiap kali pulsa dihitung
        # remaining_balance -= corrected_pulses
        # print(f"\r💳 Saldo yang masuk: Rp.{corrected_pulses*1000}", end="")
        #kondisi = input("Masukkan uang lagi?").lower

        # Cek apakah saldo sudah cukup atau berlebih
        #if remaining_balance == corrected_pulses:
        #    remaining_balance = 0  # Set saldo menjadi 0 setelah transaksi selesai
        #    transaction_active = False  # Tandai transaksi selesai
        #    pi.write(EN_PIN, 0)  # Matikan bill acceptor
        #    print(f"\r✅ Transaksi selesai!", end="")
        #    log_transaction(f"✅ Transaksi {id_trx} selesai.")

            # Kirim API bahwa transaksi sudah selesai
        #    try:
        #        print("📡 Mengirim status transaksi ke server...")
        #        response = requests.post("http://172.16.100.174:5000/api/receive",
        #                                 json={"id_trx": id_trx, "status": "success", "total_inserted": corrected_pulses*1000},
        #                                 timeout=5)
        #        print(f"✅ POST sukses: {response.status_code}, Response: {response.text}")
        #        log_transaction(f"📡 Data pulsa dikirim ke server. Status: {response.status_code}, Response: {response.text}")
        #    except requests.exceptions.RequestException as e:
        #        log_transaction(f"⚠️ Gagal mengirim status transaksi: {e}")
        #        print(f"⚠️ Gagal mengirim status transaksi: {e}")

        #elif remaining_balance > corrected_pulses:
        # Jika saldo masih kurang, lanjutkan transaksi
        #    print(f"\r💳 Tagihan sisa: Rp.{(remaining_balance-corrected_pulses*1000)}.")
        #    log_transaction(f"💳 Tagihan sisa: Rp.{remaining_balance-corrected_pulses*1000}. Masukkan sisanya.")
        #    pulse_count = 0  # Reset pulse count untuk transaksi berikutnya
        #    total_inserted = 0  # Reset total uang masuk untuk transaksi berikutnya

            # Set cooldown agar menunggu uang selanjutnya
        #    cooldown_start = time.time()

        #elif remaining_balance < corrected_pulses:
        #Jika ada kelebihan bayar, selesai transaksi
        #    corrected_pulses -= remaining_balance
        #    print(f"\r💳 Uang yang dimasukkan lebih dari cukup. Kelebihan: Rp.{corrected_pulses*1000}", end="")
        #    log_transaction(f"💳 Kelebihan bayar: Rp.{corrected_pulses*1000}. Transaksi selesai.")
        #    transaction_active = False
        #    pi.write(EN_PIN, 0)  # Matikan bill acceptor

# Endpoint untuk memulai transaksi
@app.route("/api/ba", methods=["POST"])
def trigger_transaction():
    global transaction_active, remaining_balance, id_trx, cooldown_start, total_inserted

    if transaction_active:
        return jsonify({"status": "error", "message": "Transaksi sedang berlangsung"}), 400
    
    data = request.json
    remaining_balance = int(data.get("total", 0))//1000  # Pastikan remaining_balance berupa integer
    id_trx = data.get("id_trx")
    
    if remaining_balance <= 0 or id_trx is None:
        return jsonify({"status": "error", "message": "Data tidak valid"}), 400
    

    transaction_active = True
    cooldown_start = time.time()
    total_inserted = 0  # Reset total uang yang masuk untuk transaksi baru
    log_transaction(f"🔔 Transaksi dimulai! ID: {id_trx}, Tagihan: Rp.{remaining_balance*1000}")
    print(f"Bill acceptor diaktifkan. Tagihan: Rp.{remaining_balance*1000}")
    
    pi.write(EN_PIN, 1)
    return jsonify({"status": "success", "message": "Transaksi dimulai"})

if __name__ == "__main__":
    # Pasang callback untuk pin BILL_ACCEPTOR_PIN
    #while(kondisi == "ya"):
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)
    app.run(host="0.0.0.0", port=5000, debug=True)