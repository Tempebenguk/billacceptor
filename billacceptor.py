import time
import requests
import pigpio

# Konstanta
DEBOUNCE_TIME = 0.05  # Untuk menghindari bouncing pulsa
TIMEOUT = 10  # Waktu batas dalam detik sebelum transaksi berakhir
PULSE_MAPPING = {1: 2000, 2: 5000, 3: 10000, 4: 20000, 5: 50000, 6: 100000}  # Mapping pulsa ke rupiah

# Variabel Global
pulse_count = 0
total_inserted = 0
remaining_balance = 0
transaction_active = False
last_pulse_time = 0
id_trx = None

# Inisialisasi GPIO dengan pigpio
pi = pigpio.pi()
BILL_ACCEPTOR_PIN = 17  # Sesuaikan dengan GPIO yang digunakan
EN_PIN = 27  # Pin untuk mengaktifkan bill acceptor

# Fungsi Log Transaksi
def log_transaction(message):
    with open("transaction_log.txt", "a") as log_file:
        log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {message}\n")

# Fungsi Mencari Pulsa Terdekat yang Valid
def closest_valid_pulse(pulse):
    valid_pulses = sorted(PULSE_MAPPING.keys())
    for p in valid_pulses:
        if pulse <= p:
            return p
    return None

# Fungsi Penghitungan Pulsa
def count_pulse(gpio, level, tick):
    global pulse_count, last_pulse_time, transaction_active, total_inserted, remaining_balance, id_trx

    if not transaction_active:
        return

    current_time = time.time()

    # Debounce pulsa
    if (current_time - last_pulse_time) > DEBOUNCE_TIME:
        pulse_count += 1
        last_pulse_time = current_time
        print(f"🔢 Pulsa diterima: {pulse_count}")  # Debugging untuk melihat pulsa

    # Koreksi pulsa
    corrected_pulses = closest_valid_pulse(pulse_count)
    if corrected_pulses:
        if corrected_pulses != pulse_count:
            log_transaction(f"⚠️ Koreksi pulsa: {pulse_count} dikoreksi menjadi {corrected_pulses}")

        # Konversi pulsa ke uang setelah koreksi
        received_amount = PULSE_MAPPING.get(corrected_pulses, 0)
        total_inserted += received_amount
        print(f"\r💰 Uang diterima: Rp.{received_amount}, Total masuk: Rp.{total_inserted}", end="")
        log_transaction(f"💰 Uang diterima: Rp.{received_amount}, Total masuk: Rp.{total_inserted}")

        # Pengurangan tagihan
        remaining_balance -= received_amount
        print(f"\r💳 Sisa saldo: Rp.{remaining_balance}", end="")

        # Cek apakah transaksi selesai
        if remaining_balance <= 0:
            overpaid_amount = abs(remaining_balance) if remaining_balance < 0 else 0
            remaining_balance = 0  # Reset saldo
            transaction_active = False
            pi.write(EN_PIN, 0)  # Matikan bill acceptor
            print(f"\r✅ Transaksi selesai! Kelebihan bayar: Rp.{overpaid_amount}", end="")
            log_transaction(f"✅ Transaksi {id_trx} selesai. Kelebihan bayar: Rp.{overpaid_amount}")

            # Kirim API transaksi selesai
            try:
                print("📡 Mengirim status transaksi ke server...")
                response = requests.post("http://172.16.100.160:5000/api/receive",
                                         json={"id_trx": id_trx, "status": "success", "total_inserted": total_inserted, "overpaid": overpaid_amount},
                                         timeout=5)
                print(f"✅ POST sukses: {response.status_code}, Response: {response.text}")
                log_transaction(f"📡 Data transaksi dikirim. Status: {response.status_code}, Response: {response.text}")
            except requests.exceptions.RequestException as e:
                log_transaction(f"⚠️ Gagal mengirim status transaksi: {e}")
                print(f"⚠️ Gagal mengirim status transaksi: {e}")

        pulse_count = 0  # Reset pulse count untuk transaksi berikutnya

# Fungsi untuk Memulai Transaksi
def start_transaction(amount_due, trx_id):
    global transaction_active, total_inserted, remaining_balance, id_trx, pulse_count

    transaction_active = True
    total_inserted = 0
    remaining_balance = amount_due
    id_trx = trx_id
    pulse_count = 0
    pi.write(EN_PIN, 1)  # Aktifkan bill acceptor
    print(f"🚀 Transaksi dimulai! Tagihan: Rp.{remaining_balance}, ID: {id_trx}")
    log_transaction(f"🚀 Transaksi {id_trx} dimulai. Tagihan: Rp.{remaining_balance}")

# Setup GPIO Interrupt
pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_DOWN)
pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)

# Contoh Pemanggilan
if __name__ == "__main__":
    try:
        start_transaction(20000, "TRX123")  # Contoh transaksi Rp. 20.000
        while transaction_active:
            time.sleep(1)  # Looping agar transaksi berjalan
    except KeyboardInterrupt:
        print("❌ Program dihentikan oleh pengguna.")
    finally:
        pi.stop()
