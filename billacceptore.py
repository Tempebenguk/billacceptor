import pigpio
import time
import datetime
import os
import requests
from flask import Flask, request, jsonify
import threading
import py8583

# Konfigurasi PIN GPIO
BILL_ACCEPTOR_PIN = 14  # Pin pulsa dari bill acceptor (DT)
EN_PIN = 15             # Pin enable untuk mengaktifkan bill acceptor

# Konfigurasi transaksi
TIMEOUT = 15  # Waktu maksimum transaksi sebelum cooldown
DEBOUNCE_TIME = 0.05  # Debounce
TOLERANCE = 2  # Toleransi ±2 pulsa

# Mapping jumlah pulsa ke nominal uang
PULSE_MAPPING = {
    1: 1000,
    2: 2000,
    5: 5000,
    10: 10000,
    20: 20000,
    50: 50000,
    100: 100000
}

# Lokasi penyimpanan log transaksi
LOG_DIR = "/var/www/html/logs" # Direktori untuk menyimpan log (sesuaikan dengan direktori anda)
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

# Inisialisasi Flask
app = Flask(__name__)

# Variabel Global
pulse_count = 0
last_pulse_time = time.time()
transaction_active = False
remaining_balance = 0
remaining_due = 0
id_trx = None
total_inserted = 0
last_pulse_received_time = time.time()

# Inisialisasi pigpio
pi = pigpio.pi()
if not pi.connected:
    log_transaction("Gagal terhubung ke pigpio daemon!")
    exit()

# Set mode untuk pin GPIO
pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 0)

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
        last_pulse_received_time = current_time
        print(f"Pulsa diterima: {pulse_count}")

        # Konversi pulsa ke uang
        corrected_pulses = closest_valid_pulse(pulse_count)
        if corrected_pulses:
            received_amount = PULSE_MAPPING.get(corrected_pulses, 0)
            total_inserted += received_amount
            print(f"\rTotal uang masuk: Rp.{total_inserted}", end="")
            log_transaction(f"Total uang masuk: Rp.{total_inserted}")
            pulse_count = 0

# Cooldown Timer
def start_timeout_timer():
    """Mengatur timer untuk mendeteksi timeout transaksi."""
    global total_inserted, remaining_balance, transaction_active, last_pulse_received_time, id_trx, remaining_due

    while transaction_active:
        current_time = time.time()
        remaining_time = max(0, int(TIMEOUT - (current_time - last_pulse_received_time)))
        
        if remaining_time == 0:
            pi.write(EN_PIN, 0)
            transaction_active = False
            
            if total_inserted < remaining_balance:
                remaining_due = remaining_balance - total_inserted
                print(f"\rTimeout! Kurang: Rp.{remaining_due}")
                log_transaction(f"Transaksi gagal, kurang: Rp.{remaining_due}")
                send_transaction_status("failed", total_inserted, 0, remaining_due)
            elif total_inserted == remaining_balance:
                print(f"\rTransaksi berhasil, total: Rp.{total_inserted}")
                log_transaction(f"Transaksi berhasil, total: Rp.{total_inserted}")
                send_transaction_status("success", total_inserted, 0, 0)
            else:
                overpaid = total_inserted - remaining_balance
                print(f"\rTransaksi berhasil, kelebihan: Rp.{overpaid}")
                log_transaction(f"Transaksi berhasil, kelebihan: Rp.{overpaid}")
                send_transaction_status("overpaid", total_inserted, overpaid, 0)
        
        print(f"\rTimeout dalam {remaining_time} detik...", end="")
        time.sleep(1)

        if current_time - last_pulse_received_time >= 2:
            if total_inserted >= remaining_balance:
                overpaid = total_inserted - remaining_balance
                if total_inserted == remaining_balance:
                    print(f"\rTransaksi selesai, total: Rp.{total_inserted}")
                    send_transaction_status("success", total_inserted, 0, 0)
                else:
                    print(f"\rTransaksi selesai, kelebihan: Rp.{overpaid}")
                    send_transaction_status("overpaid", total_inserted, overpaid, 0)
                transaction_active = False
                pi.write(EN_PIN, 0)
                break

# Fungsi untuk mengirim status transaksi dengan format ISO 8583
def send_transaction_status(status, total_inserted, overpaid, remaining_due):
    """Mengirim status transaksi ke server backend dalam format ISO 8583."""
    try:
        # Membuat pesan ISO 8583
        iso_message = create_iso8583_message(status, total_inserted, overpaid, remaining_due)
        
        # Kirim pesan ISO 8583 ke server
        response = requests.post("http://172.16.100.150:5000/api/receive", 
                                 data={"iso_message": iso_message}, timeout=5)
        
        print(f"POST sukses: {response.status_code}, Response: {response.text}")
        log_transaction(f"Data ISO 8583 dikirim ke server. Status: {response.status_code}, Response: {response.text}")
    except requests.exceptions.RequestException as e:
        log_transaction(f"Gagal mengirim status transaksi dalam format ISO 8583: {e}")
        print(f"Gagal mengirim status transaksi: {e}")

# Fungsi untuk membuat pesan ISO 8583
def create_iso8583_message(status, total_inserted, overpaid, remaining_due):
    """Membuat pesan ISO 8583 menggunakan pustaka py8583."""
    iso_message = py8583.ISO8583()

    # Set Message Type Indicator (MTI), misalnya 0200 untuk transaksi permintaan
    iso_message.set_type('0200')

    # Set Elemen Data
    iso_message.set(4, total_inserted)  # Jumlah transaksi
    iso_message.set(37, id_trx)         # ID Transaksi
    iso_message.set(39, status)         # Status transaksi
    iso_message.set(54, overpaid)       # Kelebihan pembayaran (jika ada)
    iso_message.set(55, remaining_due)  # Kekurangan pembayaran (jika ada)
    iso_message.set(12, datetime.datetime.now().strftime('%H%M%S'))  # Waktu transaksi
    iso_message.set(13, datetime.datetime.now().strftime('%m%d'))  # Tanggal transaksi

    # Menghasilkan pesan ISO 8583 dalam format string
    return iso_message.get_iso()

# Fungsi untuk mendapatkan pulsa yang valid
def closest_valid_pulse(pulses):
    """Mendapatkan jumlah pulsa yang paling mendekati nilai yang valid."""
    if pulses == 1:
        return 1
    if 2 < pulses < 5:
        return 2
    closest_pulse = min(PULSE_MAPPING.keys(), key=lambda x: abs(x - pulses) if x != 1 else float("inf"))
    return closest_pulse if abs(closest_pulse - pulses) <= TOLERANCE else None

# Endpoint untuk memulai transaksi
@app.route("/api/ba", methods=["POST"])
def trigger_transaction():
    """Memulai transaksi baru."""
    global transaction_active, remaining_balance, id_trx, total_inserted, last_pulse_received_time

    if transaction_active:
        return jsonify({"status": "error", "message": "Transaksi sedang berlangsung"}), 400
    
    data = request.json
    remaining_balance = int(data.get("total", 0))
    id_trx = data.get("id_trx")
    
    if remaining_balance <= 0 or id_trx is None:
        return jsonify({"status": "error", "message": "Data tidak valid"}), 400
    
    transaction_active = True
    total_inserted = 0
    last_pulse_received_time = time.time()
    
    log_transaction(f"Transaksi dimulai! ID: {id_trx}, Tagihan: Rp.{remaining_balance}")
    pi.write(EN_PIN, 1)

    threading.Thread(target=start_timeout_timer, daemon=True).start()

    return jsonify({"status": "success", "message": "Transaksi dimulai"})

if __name__ == "__main__":
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)
    app.run(host="0.0.0.0", port=5000, debug=True)
