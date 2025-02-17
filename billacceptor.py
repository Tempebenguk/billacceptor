import pigpio
import time
import datetime
import os
import requests
from flask import Flask, request, jsonify

# 📌 Konfigurasi PIN GPIO
BILL_ACCEPTOR_PIN = 14
EN_PIN = 15

# 📌 Konfigurasi transaksi
TIMEOUT = 15  # Waktu maksimum transaksi sebelum cooldown (detik)
PULSE_TIMEOUT = 0.3  # Batas waktu antara pulsa (detik)
DEBOUNCE_TIME = 0.05  # 50ms debounce
MIN_PULSE_INTERVAL = 0.04  # 40ms batas waktu pulsa minimal
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

# 📌 Variabel Global
pulse_count = 0
last_pulse_time = 0
transaction_active = False
remaining_balance = 0
id_trx = None
cooldown_start = None

def closest_valid_pulse(pulses):
    if pulses == 1:
        return 1
    if 2 < pulses < 5:
        return 2
    closest_pulse = min(PULSE_MAPPING.keys(), key=lambda x: abs(x - pulses) if x != 1 else float("inf"))
    return closest_pulse if abs(closest_pulse - pulses) <= TOLERANCE else None

def count_pulse(gpio, level, tick):
    global pulse_count, last_pulse_time, transaction_active

    if not transaction_active:
        return

    current_time = time.time()
    if (current_time - last_pulse_time) >= MIN_PULSE_INTERVAL:  # Cek interval pulsa
        pulse_count += 1
        last_pulse_time = current_time
        print(f"🔢 Pulsa diterima: {pulse_count}")

def process_payment():
    global pulse_count, remaining_balance, transaction_active
    
    while transaction_active:
        time.sleep(PULSE_TIMEOUT)
        
        if pulse_count > 0:
            corrected_pulses = closest_valid_pulse(pulse_count)
            if corrected_pulses:
                received_amount = PULSE_MAPPING.get(corrected_pulses, 0)
                print(f"💰 Total uang masuk: Rp.{received_amount}")
                remaining_balance -= received_amount
                pulse_count = 0  # Reset pulsa setelah dihitung
            
            if remaining_balance <= 0:
                print("✅ Transaksi selesai!")
                transaction_active = False
                break

# Inisialisasi Flask
app = Flask(__name__)

@app.route("/api/ba", methods=["POST"])
def trigger_transaction():
    global transaction_active, remaining_balance, id_trx, pulse_count
    
    if transaction_active:
        return jsonify({"status": "error", "message": "Transaksi sedang berlangsung"}), 400
    
    data = request.json
    remaining_balance = int(data.get("total", 0))
    id_trx = data.get("id_trx")
    
    if remaining_balance <= 0 or id_trx is None:
        return jsonify({"status": "error", "message": "Data tidak valid"}), 400
    
    transaction_active = True
    pulse_count = 0
    
    print(f"🔔 Transaksi dimulai! ID: {id_trx}, Tagihan: Rp.{remaining_balance}")
    return jsonify({"status": "success", "message": "Transaksi dimulai"})

if __name__ == "__main__":
    pi = pigpio.pi()
    if not pi.connected:
        print("⚠️ Gagal terhubung ke pigpio daemon!")
        exit()
    
    pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
    pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
    pi.set_mode(EN_PIN, pigpio.OUTPUT)
    pi.write(EN_PIN, 0)
    
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)
    
    app.run(host="0.0.0.0", port=5000, debug=True)
