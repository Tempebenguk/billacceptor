import pigpio
import time

# 📌 Konfigurasi PIN GPIO
BILL_ACCEPTOR_PIN = 14  # Pin pulsa dari bill acceptor (DT)
EN_PIN = 15             # Pin enable untuk mengaktifkan bill acceptor

# 📌 Konfigurasi transaksi
TIMEOUT = 15  # Waktu maksimum transaksi sebelum cooldown (detik)
PULSE_TIMEOUT = 0.3  # Batas waktu antara pulsa untuk menentukan akhir transaksi (detik)
DEBOUNCE_TIME = 0.05  # 50ms debounce berdasarkan debug
MIN_PULSE_INTERVAL = 0.04  # 40ms minimum interval untuk menghindari pulsa ganda
TOLERANCE = 2  # Toleransi ±2 pulsa (hanya untuk nominal Rp. 2.000 ke atas)

# 📌 Mapping jumlah pulsa ke nominal uang
PULSE_MAPPING = {
    1: 1000,   # Tanpa toleransi
    2: 2000,   # Dengan toleransi ±2
    5: 5000,   # Dengan toleransi ±2
    10: 10000, # Dengan toleransi ±2
    20: 20000, # Dengan toleransi ±2
    50: 50000, # Dengan toleransi ±2
    100: 100000 # Dengan toleransi ±2
}

# 📌 Variabel transaksi
pulse_count = 0
last_pulse_time = time.time()
last_transaction_time = time.time()
cooldown = True
total_amount = 0  # Akumulasi uang dalam sesi transaksi

# 📌 Inisialisasi pigpio
pi = pigpio.pi()

if not pi.connected:
    print("⚠️ Gagal terhubung ke pigpio daemon! Pastikan pigpiod berjalan.")
    exit()

# Pastikan EN_PIN adalah integer dan atur mode GPIO
assert isinstance(EN_PIN, int), "EN_PIN harus berupa integer!"

pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)
pi.set_mode(EN_PIN, pigpio.OUTPUT)
pi.write(EN_PIN, 1)  # Awal: Aktifkan bill acceptor

def closest_valid_pulse(pulses):
    """ Menyesuaikan jumlah pulsa dengan nominal uang terdekat menggunakan toleransi hanya untuk Rp. 2.000 ke atas """
    for valid_pulse, amount in PULSE_MAPPING.items():
        if valid_pulse == 1:  # Rp. 1.000 harus pas
            if pulses == 1:
                return 1
        else:  # Pecahan Rp. 2.000 ke atas pakai toleransi ±2
            if abs(pulses - valid_pulse) <= TOLERANCE:
                return valid_pulse
    return None  # Jika terlalu jauh dari nominal yang valid, anggap tidak valid

def count_pulse(gpio, level, tick):
    """ Callback untuk menangkap pulsa dari bill acceptor. """
    global pulse_count, last_pulse_time, last_transaction_time, cooldown, total_amount

    current_time = time.time()
    interval = current_time - last_pulse_time  # Hitung jarak antar pulsa

    # 📌 Jika sedang cooldown dan uang masuk, reset cooldown (tetapi lanjutkan akumulasi)
    if cooldown:
        print("🔄 Reset cooldown! Lanjutkan akumulasi uang.")
        cooldown = False

    # 📌 Validasi pulsa berdasarkan interval
    if interval > DEBOUNCE_TIME and interval > MIN_PULSE_INTERVAL:
        pi.write(EN_PIN, 0)  # Nonaktifkan bill acceptor segera setelah uang masuk
        pulse_count += 1
        last_pulse_time = current_time
        last_transaction_time = current_time  # Reset timer transaksi

        print(f"✅ Pulsa diterima! Interval: {round(interval, 3)} detik, Total pulsa: {pulse_count}")

# 📌 Callback untuk menangkap pulsa dari bill acceptor
pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)

print("🟢 Bill acceptor siap menerima uang...")

try:
    while True:
        current_time = time.time()

        # 📌 Jika ada pulsa masuk dan lebih dari PULSE_TIMEOUT, anggap transaksi selesai
        if pulse_count > 0 and (current_time - last_pulse_time > PULSE_TIMEOUT):
            received_pulses = pulse_count
            pulse_count = 0  # Reset penghitung pulsa untuk sesi berikutnya

            # 📌 Koreksi pulsa dengan toleransi (hanya untuk pecahan Rp. 2.000 ke atas)
            corrected_pulses = closest_valid_pulse(received_pulses)

            if corrected_pulses:
                received_amount = PULSE_MAPPING[corrected_pulses]
                total_amount += received_amount
                print(f"💰 Uang masuk: Rp.{received_amount} (Total sementara: Rp.{total_amount}) "
                      f"[Pulsa asli: {received_pulses}, Dikoreksi: {corrected_pulses}]")
            else:
                print(f"⚠️ WARNING: Pulsa tidak valid ({received_pulses} pulsa). Transaksi dibatalkan.")

            pi.write(EN_PIN, 1)  # Aktifkan kembali bill acceptor setelah transaksi selesai

        # 📌 Jika sudah melewati TIMEOUT, transaksi dianggap selesai
        if not cooldown:
            remaining_time = TIMEOUT - (current_time - last_transaction_time)
            if remaining_time > 0:
                print(f"⏳ Cooldown sisa {int(remaining_time)} detik...", end="\r")
            else:
                print("\n==========================")
                print(f"🛑 Total akhir: Rp.{total_amount}")
                print("🔻 Perangkat masuk cooldown...")
                print("==========================")

                cooldown = True
                total_amount = 0  # Reset jumlah uang setelah cooldown selesai

        time.sleep(0.1)  # Hindari penggunaan CPU berlebih

except KeyboardInterrupt:
    print("\n🛑 Program dihentikan oleh pengguna.")
    pi.stop()