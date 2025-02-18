import pigpio
import time
import datetime
import os
import requests
from flask import Flask, request, jsonify

# Konfigurasi PIN GPIO
BILL_ACCEPTOR_PIN = 14  # Pin pulsa dari bill acceptor (DT)
EN_PIN = 15             # Pin enable untuk mengaktifkan bill acceptor

# Konfigurasi transaksi
TIMEOUT = 15  # Waktu maksimum transaksi sebelum cooldown (detik)
PULSE_TIMEOUT = 0.3  # Batas waktu antara pulsa untuk menentukan akhir transaksi (detik)
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

# Lokasi penyimpanan log
LOG_FILE = "/var/www/html/logs/log.txt"
if not os.path.exists(os.path.dirname(LOG_FILE)):
    os.makedirs(os.path.dirname(LOG_FILE))

def log_transaction(message):
    timestamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    with open(LOG_FILE, "a") as log:
        log.write(f"{timestamp} {message}\n")
    print(f"{timestamp} {message}")

# Inisialisasi Flask
app = Flask(__name__)

# Variabel Global
pulse_count = 0
transaction_active = False
remaining_balance = 0
id_trx = None
total_inserted = 0
cooldown_start = None

# Inisialisasi pigpio
pi = pigpio.pi()
if not pi.connected:
    log_transaction("⚠️ Gagal terhubung ke pigpio daemon!")
    exit()

pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 0)

def closest_valid_pulse(pulses):
    closest_pulse = min(PULSE_MAPPING.keys(), key=lambda x: abs(x - pulses))
    return closest_pulse if abs(closest_pulse - pulses) <= TOLERANCE else None

def count_pulse(gpio, level, tick):
    global pulse_count, transaction_active
    if transaction_active:
        pulse_count += 1

def process_transaction():
    global pulse_count, total_inserted, remaining_balance, transaction_active
    
    if pulse_count == 0:
        return

    corrected_pulses = closest_valid_pulse(pulse_count)
    received_amount = PULSE_MAPPING.get(corrected_pulses, 0)
    
    if received_amount > 0:
        total_inserted += received_amount
        log_transaction(f"💰 Total uang masuk: Rp.{total_inserted}")
    
    pulse_count = 0  # Reset pulsa setelah dikonversi
    remaining_balance -= total_inserted
    
    if remaining_balance < 0:
        overpaid_amount = abs(remaining_balance)
        send_transaction_status("overpaid", overpaid_amount)
        reset_transaction()
    elif remaining_balance > 0:
        start_cooldown()
    else:
        send_transaction_status("success", 0)
        reset_transaction()

def start_cooldown():
    global cooldown_start
    cooldown_start = time.time()
    while time.time() - cooldown_start < TIMEOUT:
        if pulse_count > 0:
            process_transaction()
            return
    send_transaction_status("pending", remaining_balance)
    reset_transaction()

def reset_transaction():
    global transaction_active, total_inserted, remaining_balance, id_trx
    transaction_active = False
    total_inserted = 0
    remaining_balance = 0
    id_trx = None
    pi.write(EN_PIN, 0)

def send_transaction_status(status, amount):
    try:
        response = requests.post("http://172.16.100.165:5000/api/receive",
                                 json={"id_trx": id_trx, "status": status, "amount": amount},
                                 timeout=5)
        log_transaction(f"📡 Data transaksi dikirim. Status: {response.status_code}, Response: {response.text}")
    except requests.exceptions.RequestException as e:
        log_transaction(f"⚠️ Gagal mengirim status transaksi: {e}")

@app.route("/api/ba", methods=["POST"])
def trigger_transaction():
    global transaction_active, remaining_balance, id_trx

    if transaction_active:
        return jsonify({"status": "error", "message": "Transaksi sedang berlangsung"}), 400
    
    data = request.json
    remaining_balance = int(data.get("total", 0))
    id_trx = data.get("id_trx")
    
    if remaining_balance <= 0 or id_trx is None:
        return jsonify({"status": "error", "message": "Data tidak valid"}), 400
    
    transaction_active = True
    pi.write(EN_PIN, 1)
    log_transaction(f"🔔 Transaksi dimulai! ID: {id_trx}, Tagihan: Rp.{remaining_balance}")
    
    return jsonify({"status": "success", "message": "Transaksi dimulai"})

if __name__ == "__main__":
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)
    app.run(host="0.0.0.0", port=5000, debug=True)
