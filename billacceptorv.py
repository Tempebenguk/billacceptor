import asyncio
import pigpio
import time
import datetime
import os
import requests
from flask import Flask, request, jsonify

# Konfigurasi GPIO
BILL_ACCEPTOR_PIN = 14
EN_PIN = 15

# Konfigurasi transaksi
DEBOUNCE_TIME = 0.05
PULSE_TIMEOUT = 0.5  # Tunggu 0.5 detik tanpa pulsa untuk memproses
VALID_PULSES = [1, 2, 5, 10, 20, 50, 100]

# Inisialisasi Flask
app = Flask(__name__)

# Variabel Global
pulse_count = 0
transaction_active = False
total_inserted = 0
remaining_balance = 0
id_trx = None
last_pulse_time = 0
processing_task = None

pi = pigpio.pi()
if not pi.connected:
    print("⚠️ Gagal terhubung ke pigpio daemon!")
    exit()

pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 0)  # Matikan bill acceptor saat awal

# Fungsi validasi pulsa
def closest_valid_pulse(pulses):
    if pulses == 1:
        return 1
    if 2 < pulses < 5:
        return 2
    closest_pulse = min(VALID_PULSES, key=lambda x: abs(x - pulses) if x != 1 else float("inf"))
    return closest_pulse if abs(closest_pulse - pulses) <= 2 else None

async def process_pulses():
    """Menunggu hingga TB74 berhenti mengirim pulsa, lalu memprosesnya."""
    global pulse_count, total_inserted, transaction_active, processing_task

    await asyncio.sleep(PULSE_TIMEOUT)  # Tunggu sampai tidak ada pulsa baru

    if pulse_count > 0:
        corrected_pulses = closest_valid_pulse(pulse_count)
        print(f"✅ Pulsa diterima: {pulse_count} -> Koreksi: {corrected_pulses}")

        if corrected_pulses is not None:
            received_amount = corrected_pulses * 1000
            total_inserted += received_amount
            print(f"💰 Total uang masuk: Rp.{total_inserted}")

        pulse_count = 0  # Reset setelah proses

    processing_task = None  # Reset task agar bisa dipanggil lagi

def count_pulse(gpio, level, tick):
    """Menghitung pulsa dari TB74 dan memprosesnya setelah selesai."""
    global pulse_count, last_pulse_time, processing_task

    if not transaction_active:
        return

    current_time = time.time()
    if (current_time - last_pulse_time) > DEBOUNCE_TIME:
        pulse_count += 1
        last_pulse_time = current_time

        # Jika belum ada task pemrosesan, buat task baru
        if processing_task is None:
            loop = asyncio.get_running_loop()
            processing_task = loop.create_task(process_pulses())

async def start_timeout_timer():
    """Mengelola waktu timeout transaksi dan menentukan apakah transaksi berhasil atau gagal."""
    global total_inserted, remaining_balance, transaction_active

    while transaction_active:
        await asyncio.sleep(1)
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

@app.route("/api/ba", methods=["POST"])
def trigger_transaction():
    """Memulai transaksi baru."""
    global transaction_active, remaining_balance, id_trx, total_inserted

    if transaction_active:
        return jsonify({"status": "error", "message": "Transaksi sedang berlangsung"}), 400

    data = request.json
    remaining_balance = int(data.get("total", 0)) // 1000
    id_trx = data.get("id_trx")

    if remaining_balance <= 0 or id_trx is None:
        return jsonify({"status": "error", "message": "Data tidak valid"}), 400

    transaction_active = True
    total_inserted = 0
    print(f"🔔 Transaksi dimulai! ID: {id_trx}, Tagihan: Rp.{(remaining_balance*1000)}")
    pi.write(EN_PIN, 1)

    loop = asyncio.get_running_loop()
    loop.create_task(start_timeout_timer())

    return jsonify({"status": "success", "message": "Transaksi dimulai"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
    pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)
