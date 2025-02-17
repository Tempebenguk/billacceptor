import pigpio
import time
import datetime
import os
import requests
from flask import Flask, request, jsonify

# Konfigurasi PIN GPIO
BILL_ACCEPTOR_PIN = 14
EN_PIN = 15

# Konfigurasi transaksi
TIMEOUT = 15
PULSE_TIMEOUT = 0.3
DEBOUNCE_TIME = 0.05
TOLERANCE = 2
MIN_PULSE_INTERVAL = 0.04  # 40ms

PULSE_MAPPING = {
    1: 1000,
    2: 2000,
    5: 5000,
    10: 10000,
    20: 20000,
    50: 50000,
    100: 100000
}

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

# Variabel Global
pulse_count = 0
last_pulse_time = time.time()
transaction_active = False
remaining_balance = 0
id_trx = None
total_inserted = 0
cooldown_start = None

# Inisialisasi pigpio
pi = pigpio.pi()
if not pi.connected:
    log_transaction("Gagal terhubung ke pigpio daemon!")
    exit()

pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 0)

def closest_valid_pulse(pulses):
    if pulses == 1:
        return 1
    if 2 < pulses < 5:
        return 2
    closest_pulse = min(PULSE_MAPPING.keys(), key=lambda x: abs(x - pulses) if x != 1 else float("inf"))
    return closest_pulse if abs(closest_pulse - pulses) <= TOLERANCE else None

def count_pulse(gpio, level, tick):
    global pulse_count, last_pulse_time, transaction_active, total_inserted, remaining_balance, cooldown_start
    
    if not transaction_active:
        return

    current_time = time.time()
    if (current_time - last_pulse_time) < MIN_PULSE_INTERVAL:
        return

    pulse_count += 1
    last_pulse_time = current_time
    
    corrected_pulses = closest_valid_pulse(pulse_count)
    if corrected_pulses:
        received_amount = PULSE_MAPPING.get(corrected_pulses, 0)
        total_inserted += received_amount
        pulse_count = 0
        remaining_balance -= received_amount
        log_transaction(f"Total uang masuk: Rp.{total_inserted}, Saldo tersisa: Rp.{remaining_balance}")
        
        if remaining_balance <= 0:
            transaction_active = False
            pi.write(EN_PIN, 0)
            overpaid_amount = abs(remaining_balance)
            log_transaction(f"Transaksi selesai! Kelebihan: Rp.{overpaid_amount}")
            try:
                response = requests.post("http://172.16.100.160:5000/api/receive", 
                                         json={"id_trx": id_trx, "status": "success", "total_inserted": total_inserted, "overpaid": overpaid_amount}, 
                                         timeout=5)
                log_transaction(f"POST sukses: {response.status_code}, Response: {response.text}")
            except requests.exceptions.RequestException as e:
                log_transaction(f"Gagal mengirim status transaksi: {e}")
        else:
            cooldown_start = time.time()

@app.route("/api/ba", methods=["POST"])
def trigger_transaction():
    global transaction_active, remaining_balance, id_trx, cooldown_start, total_inserted
    
    if transaction_active:
        return jsonify({"status": "error", "message": "Transaksi sedang berlangsung"}), 400
    
    data = request.json
    remaining_balance = int(data.get("total", 0))
    id_trx = data.get("id_trx")
    
    if remaining_balance <= 0 or id_trx is None:
        return jsonify({"status": "error", "message": "Data tidak valid"}), 400
    
    transaction_active = True
    cooldown_start = time.time()
    total_inserted = 0
    log_transaction(f"Transaksi dimulai! ID: {id_trx}, Tagihan: Rp.{remaining_balance}")
    pi.write(EN_PIN, 1)
    return jsonify({"status": "success", "message": "Transaksi dimulai"})

if __name__ == "__main__":
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)
    app.run(host="0.0.0.0", port=5000, debug=True)
