#Import library
import pigpio
import time
import datetime
import os
import requests
from flask import Flask, request, jsonify
import asyncio

#Konfigurasi GPIO
BILL_ACCEPTOR_PIN = 14 #Pin Input Bill Acceptor
EN_PIN = 15 #Pin Output untuk aktifkan Bill Acceptor

#Konfigurasi transaksi
DEBOUNCE_TIME = 0.05
TOLERANCE = 2  # Toleransi ±2 pulsa

#List pulsa valid untuk koreksi pulsa yang ditangkap
VALID_PULSES = [1, 2, 5, 10, 20, 50, 100]

#Lokasi log transaksi
LOG_DIR = "/var/www/html/logs"
LOG_FILE = os.path.join(LOG_DIR, "log.txt")

#Pembuatan log transaksi (jika belum ada)
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

def log_transaction(message):
    """Menyimpan log transaksi ke file dan mencetak ke console."""
    timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    with open(LOG_FILE, "a") as log:
        log.write(f"{timestamp} {message}\n")
    print(f"{timestamp} {message}")

#Inisialisasi Flask
app = Flask(__name__)

#Variabel Global
pulse_count = 0
last_pulse_time = time.time()
transaction_active = False
remaining_balance = 0
remaining_due = 0  # **Sisa tagihan jika uang kurang**
id_trx = None
total_inserted = 0  # Total uang yang dimasukkan
last_pulse_received_time = time.time()  # Waktu terakhir pulsa diterima

#Inisialisasi pigpio dan konfigurasi GPIO
pi = pigpio.pi()
if not pi.connected:
    log_transaction("⚠️ Gagal terhubung ke pigpio daemon!")
    exit()
pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 0)  # Matikan bill acceptor saat awal

# Fungsi yang menghitung pulsa dari TB74
def count_pulse(gpio, level, tick):
    """Menghitung pulsa dari bill acceptor dan mengonversinya ke nominal uang."""
    global pulse_count, transaction_active, total_inserted, remaining_balance, id_trx, last_pulse_received_time

    if not transaction_active:
        return

    current_time = time.time()

    # Pastikan debounce agar tidak noise (perlu)
    if (current_time - last_pulse_received_time) > DEBOUNCE_TIME:
        pulse_count += 1
        last_pulse_received_time = current_time  # **Cooldown reset setiap pulsa masuk**

        # Menyimpan pulsa yang dikoreksi fungsi "closest_valid_pulse" ke variabel "corrected_pulse" (perlu)
        corrected_pulses = closest_valid_pulse(pulse_count)
        pulse_count = 0
        # Mengkonversi pulsa yang telah dikoreksi menjadi uang yang sesuai dengan dictionary (sepertinya tidak perlu)
        #if corrected_pulses:
        #    received_amount = PULSE_MAPPING.get(corrected_pulses, 0)
        #    total_inserted += received_amount
        #    print(f"\r💰 Total uang masuk: Rp.{total_inserted}", end="")
        #    log_transaction(f"💰 Total uang masuk: Rp.{total_inserted}")
        #    pulse_count = 0  # Reset pulse count setelah konversi

# Fungsi untuk mendapatkan pulsa yang valid (perlu)
def closest_valid_pulse(pulses):
    """Mendapatkan jumlah pulsa yang paling mendekati nilai yang valid."""
    if pulses == 1:
        return 1
    if 2 < pulses < 5:
        return 2
    closest_pulse = min(VALID_PULSES, key=lambda x: abs(x - pulses) if x != 1 else float("inf"))
    return closest_pulse if abs(closest_pulse - pulses) <= TOLERANCE else None

def start_timeout_timer():
    """Mengatur timer untuk mendeteksi timeout transaksi."""
    global total_inserted, remaining_balance, transaction_active, last_pulse_received_time, id_trx, remaining_due

    while transaction_active:
        current_time = time.time()
        remaining_time = max(0, int(TIMEOUT - (current_time - last_pulse_received_time)))  # **Integer timeout**
        
        if remaining_time == 0:
            # Timeout tercapai, matikan bill acceptor
            pi.write(EN_PIN, 0)
            transaction_active = False
            
            if total_inserted < remaining_balance:
                remaining_due = remaining_balance - total_inserted  # **Hitung sisa tagihan**
                print(f"\r⏰ Timeout! Kurang: Rp.{remaining_due}")
                log_transaction(f"⚠️ Transaksi gagal, kurang: Rp.{remaining_due}")
                send_transaction_status("failed", total_inserted, 0, remaining_due)  # **Kirim sebagai "failed" dengan sisa tagihan**
            elif total_inserted == remaining_balance:
                print(f"\r✅ Transaksi berhasil, total: Rp.{total_inserted}")
                log_transaction(f"✅ Transaksi berhasil, total: Rp.{total_inserted}")
                send_transaction_status("success", total_inserted, 0, 0)  # **Transaksi sukses**
            else:
                overpaid = total_inserted - remaining_balance
                print(f"\r✅ Transaksi berhasil, kelebihan: Rp.{overpaid}")
                log_transaction(f"✅ Transaksi berhasil, kelebihan: Rp.{overpaid}")
                send_transaction_status("overpaid", total_inserted, overpaid, 0)  # **Transaksi sukses, tapi kelebihan uang**
        
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

# **API untuk Memulai Transaksi**
@app.route("/api/ba", methods=["POST"])
def trigger_transaction():
    """Memulai transaksi baru."""
    global transaction_active, remaining_balance, id_trx, total_inserted, last_pulse_received_time

    if transaction_active:
        return jsonify({"status": "error", "message": "Transaksi sedang berlangsung"}), 400
    
    data = request.json
    remaining_balance = int(data.get("total", 0)) // 1000
    id_trx = data.get("id_trx")
    
    if remaining_balance <= 0 or id_trx is None:
        return jsonify({"status": "error", "message": "Data tidak valid"}), 400
    
    transaction_active = True
    total_inserted = 0
    last_pulse_received_time = time.time()
    
    log_transaction(f"🔔 Transaksi dimulai! ID: {id_trx}, Tagihan: Rp.{(remaining_balance*1000)}")
    pi.write(EN_PIN, 1)

    threading.Thread(target=start_timeout_timer, daemon=True).start() #Kemungkinan dihapus

    return jsonify({"status": "success", "message": "Transaksi dimulai"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)