from pathlib import Path
import subprocess
from .config import ReviewError


def create_demo(destination):
    path = Path(destination).expanduser().resolve()
    if path.exists():
        raise ReviewError("Demo destination already exists. Choose a new directory; existing files are never overwritten.")
    path.mkdir(parents=True)
    def run(*args):
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(path), *args], check=True, capture_output=True)
    run("init", "-b", "main")
    (path / "pricing.py").write_text('def calculate_total(price, quantity):\n    return price * quantity\n', encoding="utf-8")
    (path / "customers.py").write_text('def find_customer(db, email):\n    return db.execute("SELECT id FROM customers WHERE email = ?", (email,))\n', encoding="utf-8")
    (path / "README.md").write_text("# Synthetic review fixture\nDo not execute the intentionally unsafe changed functions.\n", encoding="utf-8")
    run("add", ".")
    run("-c", "user.name=Review Demo", "-c", "user.email=demo@example.invalid", "-c", "commit.gpgsign=false", "commit", "-m", "Safe baseline")
    (path / "pricing.py").write_text('def calculate_total(price, quantity):\n    return price * quantity\n\ndef evaluate_discount(expression):\n    return eval(expression)\n', encoding="utf-8")
    (path / "customers.py").write_text('def find_customer(db, email):\n    return db.execute(f"SELECT id FROM customers WHERE email = \'{email}\'")\n', encoding="utf-8")
    return path
