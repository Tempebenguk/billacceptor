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
MAX_RETRIES = 3  # Maksimal percobaan retry jika koneksi gagal
RETRY_DELAY = 3  # Delay antar retry dalam detik

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

ISO_8583_CODES = {
    "success": "00",    # Transaksi berhasil
    "failed": "05",      # Transaksi gagal
    "insufficient_funds": "51",  # Saldo tidak cukup
    "overpaid": "06"     # Kelebihan pembayaran
}

# 📌 Lokasi penyimpanan log transaksi
LOG_DIR = "/var/www/html/logs"
LOG_FILE = os.path.join(LOG_DIR, "log.txt")

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

def log_transaction(message):
    timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    with open(LOG_FILE, "a") as log:
        log.write(f"{timestamp} {message}\n")
    print(f"{timestamp} {message}")

app = Flask(__name__)

pulse_count = 0
last_pulse_time = time.time()
transaction_active = False
remaining_balance = 0
remaining_due = 0
id_trx = None
total_inserted = 0
last_pulse_received_time = time.time()

pi = pigpio.pi()
if not pi.connected:
    log_transaction("⚠️ Gagal terhubung ke pigpio daemon!")
    exit()

pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 0)

def count_pulse(gpio, level, tick):
    global pulse_count, last_pulse_time, transaction_active, total_inserted, remaining_balance, id_trx, last_pulse_received_time

    if not transaction_active:
        return

    current_time = time.time()
    if (current_time - last_pulse_time) > DEBOUNCE_TIME:
        pulse_count += 1
        last_pulse_time = current_time
        last_pulse_received_time = current_time
        corrected_pulses = closest_valid_pulse(pulse_count)
        print(f"🔢 Pulsa diterima: {pulse_count}")  # Debugging
        if corrected_pulses:
            received_amount = PULSE_MAPPING.get(corrected_pulses, 0)
            total_inserted += received_amount
            print(f"\r💰 Total uang masuk: Rp.{total_inserted}", end="")
            log_transaction(f"💰 Total uang masuk: Rp.{total_inserted}")
            pulse_count = 0

def start_timeout_timer():
    global total_inserted, remaining_balance, transaction_active, last_pulse_received_time, id_trx, remaining_due

    while transaction_active:
        current_time = time.time()
        remaining_time = max(0, int(TIMEOUT - (current_time - last_pulse_received_time)))
        if remaining_time == 0:
            pi.write(EN_PIN, 0)
            transaction_active = False
            
            if total_inserted < remaining_balance:
                remaining_due = remaining_balance - total_inserted
                print(f"\r⏰ Timeout! Kurang: Rp.{remaining_due}")
                log_transaction(f"⚠️ Transaksi gagal, kurang: Rp.{remaining_due}")
                send_transaction_status("failed", total_inserted, 0, remaining_due)
            elif total_inserted == remaining_balance:
                print(f"\r✅ Transaksi berhasil, total: Rp.{total_inserted}")
                log_transaction(f"✅ Transaksi berhasil, total: Rp.{total_inserted}")
                send_transaction_status("success", total_inserted, 0, 0)
            else:
                overpaid = total_inserted - remaining_balance
                print(f"\r✅ Transaksi berhasil, kelebihan: Rp.{overpaid}")
                log_transaction(f"✅ Transaksi berhasil, kelebihan: Rp.{overpaid}")
                send_transaction_status("overpaid", total_inserted, overpaid, 0)
        print(f"\r⏳ Timeout dalam {remaining_time} detik...", end="")  # **Tampilkan sebagai integer**
        time.sleep(1)

        # **Logika pengecekan setelah 2 detik**
        if current_time - last_pulse_received_time >= 2:
            if total_inserted >= remaining_balance:
                # Jika uang sudah cukup atau lebih, langsung kirim status transaksi dan hentikan transaksi
                overpaid = total_inserted - remaining_balance
                if total_inserted == remaining_balance:
                    print(f"\r✅ Transaksi selesai, total: Rp.{total_inserted}")
                    send_transaction_status("success", total_inserted, 0, 0)  # Transaksi sukses
                else:
                    print(f"\r✅ Transaksi selesai, kelebihan: Rp.{overpaid}")
                    send_transaction_status("overpaid", total_inserted, overpaid, 0)  # Transaksi sukses, tapi kelebihan uang
                transaction_active = False
                pi.write(EN_PIN, 0)  # Matikan bill acceptor

                break

def send_transaction_status(status, total_inserted, overpaid, remaining_due):
    iso_code = ISO_8583_CODES.get(status, "05")
    for attempt in range(MAX_RETRIES):
        try:
            print("📡 Mengirim status transaksi ke server...")
            response = requests.post("http://172.16.100.150:5000/api/receive",
                                     json={"id_trx": id_trx, "status": status, "iso_code": iso_code, "total_inserted": total_inserted, "overpaid": overpaid, "remaining_due": remaining_due},
                                     timeout=5)
            print(f"✅ POST sukses: {response.status_code}, Response: {response.text}")
            log_transaction(f"📡 Data dikirim ke server. Status: {response.status_code}, Response: {response.text}")
            return
        except requests.exceptions.RequestException as e:
            log_transaction(f"⚠️ Gagal mengirim status transaksi (Percobaan {attempt+1}/{MAX_RETRIES}): {e}")
            time.sleep(RETRY_DELAY)
    log_transaction("❌ Gagal mengirim data setelah maksimal percobaan.")
    print(f"⚠️ Gagal mengirim status transaksi: {e}")

def closest_valid_pulse(pulses):
    if pulses == 1:
        return 1
    if 2 < pulses < 5:
        return 2
    closest_pulse = min(PULSE_MAPPING.keys(), key=lambda x: abs(x - pulses) if x != 1 else float("inf"))
    return closest_pulse if abs(closest_pulse - pulses) <= TOLERANCE else None

@app.route("/api/ba", methods=["POST"])
def trigger_transaction():
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
    log_transaction(f"🔔 Transaksi dimulai! ID: {id_trx}, Tagihan: Rp.{remaining_balance}")
    pi.write(EN_PIN, 1)
    threading.Thread(target=start_timeout_timer, daemon=True).start()
    return jsonify({"status": "success", "message": "Transaksi dimulai"})

if __name__ == "__main__":
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)
    app.run(host="0.0.0.0", port=5000, debug=True)