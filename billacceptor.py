import time
import pigpio

# Inisialisasi
BILL_ACCEPTOR_PIN = 14
DEBOUNCE_TIME = 0.05  # debounce dalam detik
PULSE_TIMEOUT = 0.3  # timeout untuk menghitung pulsa dalam detik
pulse_count = 0
last_pulse_time = time.time()

# Fungsi untuk menghitung pulsa
def count_pulse(gpio, level, tick):
    global pulse_count, last_pulse_time

    current_time = time.time()

    # Menghindari pulsa yang terhitung ganda dengan debounce
    if (current_time - last_pulse_time) > DEBOUNCE_TIME:
        pulse_count += 1
        print(f"🔢 Pulsa diterima: {pulse_count}")
        last_pulse_time = current_time

# Fungsi untuk memeriksa PULSE_TIMEOUT
def check_timeout():
    global pulse_count, last_pulse_time

    current_time = time.time()
    # Cek apakah timeout tercapai
    if (current_time - last_pulse_time) > PULSE_TIMEOUT:
        print(f"⏰ Timeout tercapai setelah {PULSE_TIMEOUT} detik tanpa pulsa.")
        if pulse_count > 0:
            print(f"💰 Total pulsa yang dihitung: {pulse_count}")
        else:
            print("⚠️ Tidak ada pulsa yang diterima.")
        # Reset pulse_count untuk percakapan berikutnya
        pulse_count = 0
        last_pulse_time = current_time

# Inisialisasi pigpio
pi = pigpio.pi()
if not pi.connected:
    print("⚠️ Gagal terhubung ke pigpio daemon!")
    exit()

pi.set_mode(BILL_ACCEPTOR_PIN, pigpio.INPUT)
pi.set_pull_up_down(BILL_ACCEPTOR_PIN, pigpio.PUD_UP)

# Callback untuk mendeteksi pulsa
pi.callback(BILL_ACCEPTOR_PIN, pigpio.RISING_EDGE, count_pulse)

# Fungsi utama untuk terus memantau
def main():
    while True:
        # Periksa timeout setiap detik
        check_timeout()
        time.sleep(1)

if __name__ == "__main__":
    main()