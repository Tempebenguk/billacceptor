import pigpio
import time
import datetime
import os
import requests
from flask import Flask, request, jsonify
import threading

# 📌 Konfigurasi PIN GPIO
BILL_ACCEPTOR_PIN = 14
EN_PIN = 15  

# 📌 Konfigurasi transaksi
TIMEOUT = 15  
DEBOUNCE_TIME = 0.05  
TOLERANCE = 2  

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

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

def log_transaction(message):
    timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    with open(LOG_FILE, "a") as log:
        log.write(f"{timestamp} {message}\n")
    print(f"{timestamp} {message}")

app = Flask(__name__)

# 📌 Variabel Global
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
        if corrected_pulses:
            received_amount = PULSE_MAPPING.get(corrected_pulses, 0)
            total_inserted += received_amount
            print(f"\r💰 Total uang masuk: Rp.{total_inserted}", end="")
            log_transaction(f"💰 Total uang masuk: Rp.{total_inserted}")
            pulse_count = 0  

        threading.Thread(target=check_transaction_status, daemon=True).start()  

def check_transaction_status():
    global transaction_active, total_inserted, remaining_balance, remaining_due

    time.sleep(2)  

    if total_inserted < remaining_balance:
        remaining_due = remaining_balance - total_inserted  
        print(f"\r⚠️ Uang kurang Rp.{remaining_due}, menunggu tambahan (Timeout 15 detik)...")
        log_transaction(f"⚠️ Uang kurang Rp.{remaining_due}, timeout dalam 15 detik")
        start_timeout_timer()  
    elif total_inserted == remaining_balance:
        print(f"\r✅ Transaksi berhasil, total: Rp.{total_inserted}")
        log_transaction(f"✅ Transaksi berhasil, total: Rp.{total_inserted}")
        send_transaction_status("success", total_inserted, 0, 0)
        transaction_active = False
        pi.write(EN_PIN, 0)
    else:
        overpaid = total_inserted - remaining_balance
        print(f"\r✅ Transaksi berhasil, kelebihan: Rp.{overpaid}")
        log_transaction(f"✅ Transaksi berhasil, kelebihan: Rp.{overpaid}")
        send_transaction_status("overpaid", total_inserted, overpaid, 0)
        transaction_active = False
        pi.write(EN_PIN, 0)

def start_timeout_timer():
    global total_inserted, remaining_balance, transaction_active, last_pulse_received_time, id_trx, remaining_due

    timeout_start = time.time()

    while transaction_active:
        current_time = time.time()
        remaining_time = max(0, int(TIMEOUT - (current_time - timeout_start)))  
        
        if remaining_time == 0:
            pi.write(EN_PIN, 0)
            transaction_active = False
            print(f"\r⏰ Timeout! Kurang: Rp.{remaining_due}")
            log_transaction(f"⚠️ Transaksi gagal, kurang: Rp.{remaining_due}")
            send_transaction_status("failed", total_inserted, 0, remaining_due)  
            return
        
        print(f"\r⏳ Timeout dalam {remaining_time} detik...", end="")
        time.sleep(1)

def send_transaction_status(status, total_inserted, overpaid, remaining_due):
    try:
        print("📡 Mengirim status transaksi ke server...")
        response = requests.post("http://172.16.100.165:5000/api/receive",
                                 json={"id_trx": id_trx, "status": status, "total_inserted": total_inserted, "overpaid": overpaid, "remaining_due": remaining_due},
                                 timeout=5)
        print(f"✅ POST sukses: {response.status_code}, Response: {response.text}")
        log_transaction(f"📡 Data dikirim ke server. Status: {response.status_code}, Response: {response.text}")
    except requests.exceptions.RequestException as e:
        log_transaction(f"⚠️ Gagal mengirim status transaksi: {e}")
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

    return jsonify({"status": "success", "message": "Transaksi dimulai"})

if __name__ == "__main__":
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)
    app.run(host="0.0.0.0", port=5000, debug=True)
