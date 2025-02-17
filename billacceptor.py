import pigpio
import time
import datetime
import os
from flask import Flask, request, jsonify

# 📌 Konfigurasi PIN GPIO
BILL_ACCEPTOR_PIN = 14  # Pin pulsa dari bill acceptor (DT)
EN_PIN = 15             # Pin enable untuk mengaktifkan bill acceptor

# 📌 Konfigurasi transaksi
TIMEOUT = 15  # Waktu maksimum transaksi sebelum cooldown (detik)
PULSE_TIMEOUT = 0.3  # Batas waktu antara pulsa untuk menentukan akhir transaksi (detik)
DEBOUNCE_TIME = 0.05  # 50ms debounce berdasarkan debug
MIN_PULSE_INTERVAL = 0.04  # 40ms minimum interval untuk menghindari pulsa ganda
TOLERANCE = 2  # Toleransi ±2 pulsa (kecuali Rp. 1.000 & Rp. 2.000)

# 📌 Mapping jumlah pulsa ke nominal uang
PULSE_MAPPING = {
    1: 1000,   # Tanpa toleransi
    2: 2000,   # Dengan toleransi khusus (3-4 pulsa tetap 2)
    5: 5000,   # Dengan toleransi ±2
    10: 10000, # Dengan toleransi ±2
    20: 20000, # Dengan toleransi ±2
    50: 50000, # Dengan toleransi ±2
    100: 100000 # Dengan toleransi ±2
}

# 📌 Lokasi penyimpanan log
LOG_DIR = "/var/www/html/logs"
LOG_FILE = os.path.join(LOG_DIR, "log.txt")

# 📌 Buat folder `logs/` jika belum ada
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)
    print(f"📁 Folder log dibuat: {LOG_DIR}")

# 📌 Buat file log jika belum ada
if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, "w") as log:
        log.write("=== LOG TRANSAKSI BILL ACCEPTOR ===\n")
    print(f"📝 File log dibuat: {LOG_FILE}")

# 📌 Fungsi Logging
def log_transaction(message):
    """ Fungsi untuk menulis log ke file """
    with open(LOG_FILE, "a") as log:
        timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
        log.write(f"{timestamp} {message}\n")

# 📌 Variabel transaksi
pulse_count = 0
last_pulse_time = time.time()
last_transaction_time = time.time()
cooldown = True
total_amount = 0  # Akumulasi uang dalam sesi transaksi
first_transaction_time = None  # Waktu transaksi pertama kali

# 📌 Inisialisasi pigpio
pi = pigpio.pi()

if not pi.connected:
    log_transaction("⚠️ Gagal terhubung ke pigpio daemon! Pastikan pigpiod berjalan.")
    print("⚠️ Gagal terhubung ke pigpio daemon! Pastikan pigpiod berjalan.")
    exit()

pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 1)  # Awal: Aktifkan bill acceptor

# 📌 Variabel transaksi global untuk Flask
transaction_active = False
remaining_balance = 0
id_trx = None
cooldown_start = None
total_inserted = 0

# 📌 Fungsi koreksi pulsa dengan toleransi ±2
def closest_valid_pulse(pulses):
    """ Koreksi jumlah pulsa dengan toleransi ±2 kecuali untuk Rp. 1000 dan Rp. 2000 """
    if pulses == 1:
        return 1  # Rp. 1000 harus pas
    
    # Toleransi khusus untuk Rp. 2000
    if 2 < pulses < 5:
        return 2  # Koreksi ke 2 jika antara 3-4 pulsa

    closest_pulse = None
    min_diff = float("inf")

    for valid_pulse in PULSE_MAPPING.keys():
        if valid_pulse != 1 and abs(pulses - valid_pulse) <= TOLERANCE:
            diff = abs(pulses - valid_pulse)
            if diff < min_diff:
                min_diff = diff
                closest_pulse = valid_pulse

    return closest_pulse  # Mengembalikan pulsa yang sudah dikoreksi atau None jika tidak valid

# 📌 Callback untuk menangkap pulsa dari bill acceptor
def count_pulse(gpio, level, tick):
    """ Callback untuk menangkap pulsa dari bill acceptor """
    global pulse_count, last_pulse_time, last_transaction_time, cooldown, total_amount, first_transaction_time, total_inserted

    current_time = time.time()
    interval = current_time - last_pulse_time

    if cooldown:
        print("🔄 Reset cooldown! Lanjutkan akumulasi uang.")
        cooldown = False
        first_transaction_time = datetime.datetime.now()
        log_transaction(f"🕒 Transaksi pertama kali dimulai pada {first_transaction_time}")

    if interval > DEBOUNCE_TIME and interval > MIN_PULSE_INTERVAL:
        pi.write(EN_PIN, 0)  # Nonaktifkan bill acceptor segera setelah uang masuk
        pulse_count += 1
        last_pulse_time = current_time
        last_transaction_time = current_time

        print(f"✅ Pulsa diterima! Interval: {round(interval, 3)} detik, Total pulsa: {pulse_count}")
        
        # Konversi pulsa ke uang dan tambahkan ke total_amount
        valid_pulse = closest_valid_pulse(pulse_count)
        if valid_pulse:
            amount = PULSE_MAPPING[valid_pulse]
            total_inserted += amount
            print(f"💰 Uang yang dimasukkan: Rp.{amount}, Total uang yang dimasukkan: Rp.{total_inserted}")
            log_transaction(f"💰 Uang yang dimasukkan: Rp.{amount}, Total uang yang dimasukkan: Rp.{total_inserted}")

# 📌 Flask app
app = Flask(__name__)

@app.route("/api/ba", methods=["POST"])
def trigger_transaction():
    global transaction_active, remaining_balance, id_trx, cooldown_start, total_inserted

    if transaction_active:
        return jsonify({"status": "error", "message": "Transaksi sedang berlangsung"}), 400
    
    data = request.json
    remaining_balance = int(data.get("total", 0))  # Pastikan remaining_balance berupa integer
    id_trx = data.get("id_trx")
    
    if remaining_balance <= 0 or id_trx is None:
        return jsonify({"status": "error", "message": "Data tidak valid"}), 400
    
    transaction_active = True
    cooldown_start = time.time()
    total_inserted = 0  # Reset total uang yang masuk untuk transaksi baru
    log_transaction(f"🔔 Transaksi dimulai! ID: {id_trx}, Tagihan: Rp.{remaining_balance}")
    print(f"Bill acceptor diaktifkan. Tagihan: Rp.{remaining_balance}")
    
    pi.write(EN_PIN, 1)  # Mengaktifkan bill acceptor untuk menerima uang
    
    return jsonify({"status": "success", "message": "Transaksi dimulai"})

@app.route("/api/ba/status", methods=["GET"])
def get_transaction_status():
    """ Endpoint untuk memantau status transaksi dan sisa tagihan """
    global remaining_balance, total_inserted, transaction_active

    if transaction_active:
        remaining_balance -= total_inserted  # Kurangi sisa tagihan berdasarkan total uang yang masuk
        total_inserted = 0  # Reset jumlah uang yang baru dimasukkan

        if remaining_balance <= 0:
            # Jika tagihan sudah terbayar sepenuhnya
            transaction_active = False
            pi.write(EN_PIN, 0)  # Nonaktifkan bill acceptor
            log_transaction(f"✅ Transaksi berhasil! ID: {id_trx}, Total bayar: Rp.{remaining_balance}")
            print(f"Transaksi selesai. Total bayar: Rp.{remaining_balance}")
            return jsonify({"status": "success", "message": "Tagihan terbayar", "id_trx": id_trx}), 200
        
        # Kirim status terkini, sisa tagihan dan uang yang sudah masuk
        return jsonify({
            "status": "pending",
            "message": "Transaksi berlangsung",
            "remaining_balance": remaining_balance,
            "total_inserted": total_inserted
        }), 200
    else:
        return jsonify({"status": "error", "message": "Tidak ada transaksi aktif"}), 400

if __name__ == "__main__":
    # Pasang callback untuk pin BILL_ACCEPTOR_PIN
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)
    
    app.run(host="0.0.0.0", port=5000, debug=True)
