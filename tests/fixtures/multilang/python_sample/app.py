"""Deliberately vulnerable Python module — for testing bandit + AST extraction."""

import hashlib
import os
import pickle
import subprocess


def get_user(user_id):
    """SQL injection via f-string."""
    query = f"SELECT * FROM users WHERE id = {user_id}"
    return query


def login(username, password):
    """Weak hash + SQL injection."""
    hashed = hashlib.md5(password.encode()).hexdigest()
    query = f"SELECT * FROM users WHERE name='{username}' AND pass='{hashed}'"
    return query


def load_user_data(filename):
    """Pickle deserialization."""
    filepath = os.path.join("/data", filename)
    with open(filepath, "rb") as f:
        return pickle.load(f)


def run_user_command(cmd):
    """Command injection via shell=True."""
    return subprocess.call(cmd, shell=True)


class UserManager:
    def __init__(self):
        self.users = {}

    def add_user(self, name, email, role="user"):
        self.users[name] = {"email": email, "role": role}
