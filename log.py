import os
import subprocess
from datetime import datetime

log_file = "/var/www/html/logs/log.txt"

date_str = datetime.now().strftime('%Y-%m-%d')
renamed_log_file = f"/var/www/html/logs/log_{date_str}.txt"

os.rename(log_file, renamed_log_file)

subprocess.run(["git", "add", renamed_log_file])

subprocess.run(["git", "commit", "-m", f"Update Log {date_str}"])

subprocess.run(["git", "push", "origin", "main"])

with open(log_file, 'w') as f:
    pass

os.remove(renamed_log_file)
