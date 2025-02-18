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
TIMEOUT = 15  # Timeout setelah pulsa terakhir jika uang masih kurang
DEBOUNCE_TIME = 0.05  
TOLERANCE = 2  
PULSE_WAIT_TIME = 10  # Tunggu 10 detik setelah pulsa terakhir sebelum evaluasi transaksi pertama

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
sent_status = False  
timeout_thread = None  
first_evaluation_done = False  


def count_pulse(gpio, level, tick):
    global pulse_count, last_pulse_time, total_inserted, transaction_active, sent_status, timeout_thread, first_evaluation_done, remaining_due

    if not transaction_active:
        return

    current_time = time.time()
    if (current_time - last_pulse_time) > DEBOUNCE_TIME:
        pulse_count += 1
        last_pulse_time = current_time  

        corrected_pulses = closest_valid_pulse(pulse_count)
        if corrected_pulses:
            received_amount = PULSE_MAPPING.get(corrected_pulses, 0)
            total_inserted += received_amount
            log_transaction(f"💰 Total uang masuk: Rp.{total_inserted}")
            pulse_count = 0  

        # Perbarui remaining_due
        remaining_due = max(0, remaining_balance - total_inserted)

        if first_evaluation_done:
            reset_timeout()  
        elif not sent_status:
            sent_status = True
            threading.Thread(target=delayed_transaction_check, daemon=True).start()


def delayed_transaction_check():
    global transaction_active, total_inserted, remaining_balance, remaining_due, sent_status, first_evaluation_done

    time.sleep(PULSE_WAIT_TIME)  

    if total_inserted < remaining_balance:
        remaining_due = remaining_balance - total_inserted
        log_transaction(f"⚠️ Uang kurang Rp.{remaining_due}, timeout dalam {TIMEOUT} detik")
        first_evaluation_done = True  
        reset_timeout()  
    else:
        process_final_transaction()

    sent_status = False


def reset_timeout():
    global timeout_thread

    if timeout_thread:
        timeout_thread.cancel()  

    timeout_thread = threading.Thread(target=timeout_countdown, daemon=True)
    timeout_thread.start()


def timeout_countdown():
    global transaction_active, remaining_due, total_inserted

    for i in range(TIMEOUT, 0, -1):
        if not transaction_active or remaining_due == 0:  
            return
        log_transaction(f"⌛ Timeout dalam {i} detik... (Uang masuk: Rp.{total_inserted}, Sisa: Rp.{remaining_due})")
        time.sleep(1)

    process_final_transaction()


def process_final_transaction():
    global transaction_active, remaining_due, total_inserted

    if transaction_active:
        transaction_active = False
        pi.write(EN_PIN, 0)

        overpaid = max(0, total_inserted - remaining_balance)
        remaining_due = 0 if total_inserted >= remaining_balance else remaining_balance - total_inserted

        status = "success" if remaining_due == 0 else "failed"
        log_transaction(f"✅ Transaksi {status}, total: Rp.{total_inserted}, Kelebihan: Rp.{overpaid}, Sisa tagihan: Rp.{remaining_due}")
        send_transaction_status(status, total_inserted, overpaid, remaining_due)


def send_transaction_status(status, total_inserted, overpaid, remaining_due):
    try:
        response = requests.post("http://172.16.100.165:5000/api/receive",
                                 json={"id_trx": id_trx, "status": status, "total_inserted": total_inserted, "overpaid": overpaid, "remaining_due": remaining_due},
                                 timeout=5)
        log_transaction(f"📡 Data dikirim ke server. Status: {response.status_code}, Response: {response.text}")
    except requests.exceptions.RequestException as e:
        log_transaction(f"⚠️ Gagal mengirim status transaksi: {e}")


def closest_valid_pulse(pulses):
    if pulses == 1:
        return 1
    if 2 < pulses < 5:
        return 2
    closest_pulse = min(PULSE_MAPPING.keys(), key=lambda x: abs(x - pulses) if x != 1 else float("inf"))
    return closest_pulse if abs(closest_pulse - pulses) <= TOLERANCE else None


@app.route("/api/ba", methods=["POST"])
def trigger_transaction():
    global transaction_active, remaining_balance, id_trx, total_inserted, sent_status, timeout_thread, first_evaluation_done, remaining_due

    if transaction_active:
        return jsonify({"status": "error", "message": "Transaksi sedang berlangsung"}), 400
    
    data = request.json
    remaining_balance = int(data.get("total", 0))
    id_trx = data.get("id_trx")
    
    if remaining_balance <= 0 or id_trx is None:
        return jsonify({"status": "error", "message": "Data tidak valid"}), 400
    
    transaction_active = True
    total_inserted = 0
    sent_status = False
    first_evaluation_done = False  
    remaining_due = remaining_balance  

    log_transaction(f"🔔 Transaksi dimulai! ID: {id_trx}, Tagihan: Rp.{remaining_balance}")
    pi.write(EN_PIN, 1)

    if timeout_thread:
        timeout_thread.cancel()

    return jsonify({"status": "success", "message": "Transaksi dimulai"})


if __name__ == "__main__":
    pi = pigpio.pi()
    if not pi.connected:
        log_transaction("⚠️ Gagal terhubung ke pigpio daemon!")
        exit()
    pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
    pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
    pi.set_mode(EN_PIN, pigpio.OUTPUT)
    pi.write(EN_PIN, 0)
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)
    app.run(host="0.0.0.0", port=5000, debug=True)
