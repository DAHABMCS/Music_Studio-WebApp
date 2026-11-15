import json
import os
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash

USERS_FILE = Path(__file__).parent / "users.json"

def load_users():
    if not USERS_FILE.exists():
        # Create default admin:admin  -> change immediately!
        default = {
            "admin": {
                "password": generate_password_hash("admin"),
                "role": "admin"
            }
        }
        USERS_FILE.write_text(json.dumps(default, indent=2))
        return default
    return json.loads(USERS_FILE.read_text())

def verify_user(username: str, password: str) -> bool:
    users = load_users()
    user = users.get(username)
    if not user:
        return False
    return check_password_hash(user["password"], password)

def add_user(username: str, password: str, role: str = "user"):
    users = load_users()
    users[username] = {
        "password": generate_password_hash(password),
        "role": role
    }
    USERS_FILE.write_text(json.dumps(users, indent=2))

def user_exists(username: str) -> bool:
    return username in load_users()