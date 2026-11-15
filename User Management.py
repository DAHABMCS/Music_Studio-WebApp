#!/usr/bin/env python3
"""
manage_users_gui.py — standalone Tkinter GUI for managing a users.json file.

users.json format:
{
  "admin": {
    "password": "scrypt:32768:8:1$salt$hash",
    "role": "admin"
  },
  "someuser": {
    "password": "scrypt:32768:8:1$salt$hash",
    "role": "user"
  }
}

Uses Werkzeug's scrypt-based password hashing (same format already in your file).

Dependencies:
    pip install werkzeug --break-system-packages

Run:
    python3 manage_users_gui.py [path/to/users.json]

Tkinter ships with standard Python installs on Windows/macOS. On Linux you may
need to install it separately, e.g.:
    sudo apt install python3-tk
"""

import json
import os
import shutil
import sys
import tkinter as tk
from datetime import datetime
from tkinter import ttk, messagebox, simpledialog

try:
    from werkzeug.security import generate_password_hash, check_password_hash
except ImportError:
    print(
        "Missing dependency: werkzeug.\n"
        "Install it with:  pip install werkzeug --break-system-packages",
        file=sys.stderr,
    )
    sys.exit(1)

DEFAULT_FILE = "users.json"


def default_users_path() -> str:
    """users.json living next to this script."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, DEFAULT_FILE)


# --------------------------------------------------------------------------
# Storage helpers (same logic as the CLI version)
# --------------------------------------------------------------------------

def load_users(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        if not content:
            return {}
        return json.loads(content)


def save_users(path: str, users: dict) -> None:
    if os.path.exists(path):
        backup = f"{path}.bak.{datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(path, backup)

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


def hash_password(plain: str) -> str:
    return generate_password_hash(plain, method="scrypt")


# --------------------------------------------------------------------------
# Small modal dialog for entering (and confirming) a password
# --------------------------------------------------------------------------

class EditUserDialog(simpledialog.Dialog):
    """
    Unified add/edit dialog: username, role, and an optional password change.
    - existing_username=None  -> "Add user" mode (password required)
    - existing_username=name  -> "Edit user" mode (username can be renamed,
      password change is optional via checkbox)
    """

    def __init__(self, parent, existing_username=None, current_role="user"):
        self.existing_username = existing_username
        self.current_role = current_role
        self.result_username = None
        self.result_role = None
        self.result_password = None  # None = leave unchanged (edit mode only)
        title = f"Edit user '{existing_username}'" if existing_username else "Add user"
        super().__init__(parent, title=title)

    def body(self, master):
        row = 0
        ttk.Label(master, text="Username:").grid(row=row, column=0, sticky="w", pady=4)
        self.username_entry = ttk.Entry(master, width=30)
        self.username_entry.grid(row=row, column=1, pady=4)
        if self.existing_username:
            self.username_entry.insert(0, self.existing_username)
        row += 1

        ttk.Label(master, text="Role:").grid(row=row, column=0, sticky="w", pady=4)
        self.role_entry = ttk.Entry(master, width=30)
        self.role_entry.grid(row=row, column=1, pady=4)
        self.role_entry.insert(0, self.current_role)
        row += 1

        self.change_pw_var = tk.BooleanVar(value=(self.existing_username is None))
        if self.existing_username:
            chk = ttk.Checkbutton(
                master, text="Change password", variable=self.change_pw_var,
                command=self._toggle_pw_fields
            )
            chk.grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
            row += 1

        ttk.Label(master, text="Password:").grid(row=row, column=0, sticky="w", pady=4)
        self.pw1 = ttk.Entry(master, show="*", width=30)
        self.pw1.grid(row=row, column=1, pady=4)
        row += 1

        ttk.Label(master, text="Confirm:").grid(row=row, column=0, sticky="w", pady=4)
        self.pw2 = ttk.Entry(master, show="*", width=30)
        self.pw2.grid(row=row, column=1, pady=4)
        row += 1

        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            master, text="Show password", variable=self.show_var, command=self._toggle_show
        ).grid(row=row, column=0, columnspan=2, sticky="w")

        self._toggle_pw_fields()
        return self.username_entry

    def _toggle_pw_fields(self):
        state = "normal" if self.change_pw_var.get() else "disabled"
        self.pw1.config(state=state)
        self.pw2.config(state=state)

    def _toggle_show(self):
        char = "" if self.show_var.get() else "*"
        self.pw1.config(show=char)
        self.pw2.config(show=char)

    def validate(self):
        username = self.username_entry.get().strip()
        role = self.role_entry.get().strip()

        if not username:
            messagebox.showerror("Error", "Username cannot be empty.", parent=self)
            return False
        if not role:
            messagebox.showerror("Error", "Role cannot be empty.", parent=self)
            return False

        password = None
        if self.change_pw_var.get():
            p1, p2 = self.pw1.get(), self.pw2.get()
            if not p1:
                messagebox.showerror("Error", "Password cannot be empty.", parent=self)
                return False
            if p1 != p2:
                messagebox.showerror("Error", "Passwords do not match.", parent=self)
                return False
            password = p1
        elif self.existing_username is None:
            # add mode always requires a password
            messagebox.showerror("Error", "Password is required for a new user.", parent=self)
            return False

        self.result_username = username
        self.result_role = role
        self.result_password = password
        return True


# --------------------------------------------------------------------------
# Main application
# --------------------------------------------------------------------------

class UserManagerApp(tk.Tk):
    def __init__(self, path: str):
        super().__init__()
        self.title("User Manager")
        self.geometry("640x420")
        self.minsize(520, 360)

        self.path = path
        self.users = {}
        self.dirty = False  # True whenever self.users has changes not yet written to disk

        self._build_menu()
        self._build_widgets()
        self._reload()

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---- UI construction ----

    def _build_menu(self):
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Reload", command=self._reload)
        file_menu.add_separator()
        file_menu.add_command(label="Save", command=lambda: self._save() and self._set_status("Saved."))
        file_menu.add_command(label="Quit", command=self.on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        self.config(menu=menubar)

    def _build_widgets(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        self.path_label = ttk.Label(top, text=self.path, foreground="#555")
        self.path_label.pack(side="left")

        # Table
        columns = ("username", "role", "hash")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("username", text="Username")
        self.tree.heading("role", text="Role")
        self.tree.heading("hash", text="Password hash (truncated)")
        self.tree.column("username", width=140, anchor="w")
        self.tree.column("role", width=100, anchor="w")
        self.tree.column("hash", width=340, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.tree.bind("<Double-1>", lambda e: self.action_edit())

        # Buttons
        btns = ttk.Frame(self, padding=(8, 0, 8, 8))
        btns.pack(fill="x")

        ttk.Button(btns, text="Add user", command=self.action_add).pack(side="left")
        ttk.Button(btns, text="Edit user", command=self.action_edit).pack(side="left", padx=6)
        ttk.Button(btns, text="Delete user", command=self.action_delete).pack(side="left")
        ttk.Button(btns, text="Verify password", command=self.action_verify).pack(side="left", padx=6)

        # Status bar
        self.status_var = tk.StringVar(value="Ready.")
        status = ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w", padding=4)
        status.pack(fill="x", side="bottom")

    # ---- data / status helpers ----

    def _set_status(self, msg: str):
        self.status_var.set(msg)

    def _reload(self):
        try:
            self.users = load_users(self.path)
        except Exception as e:
            messagebox.showerror("Error loading file", str(e))
            self.users = {}
        self.path_label.config(text=self.path)
        self._refresh_table()
        self._set_status(f"Loaded {len(self.users)} user(s) from {self.path}")

    def _refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        for name, info in sorted(self.users.items()):
            role = info.get("role", "?")
            pw = info.get("password", "")
            short = pw[:40] + "..." if len(pw) > 40 else pw
            self.tree.insert("", "end", iid=name, values=(name, role, short))

    def _save(self):
        try:
            save_users(self.path, self.users)
        except Exception as e:
            messagebox.showerror("Error saving file", str(e))
            return False
        self.dirty = False
        return True

    def _selected_username(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("No selection", "Select a user in the table first.")
            return None
        return sel[0]

    # ---- actions ----

    def action_add(self):
        dlg = EditUserDialog(self, existing_username=None, current_role="user")
        if dlg.result_username is None:
            return  # cancelled

        username = dlg.result_username
        if username in self.users:
            messagebox.showerror("Error", f"User '{username}' already exists.")
            return

        self.users[username] = {
            "password": hash_password(dlg.result_password),
            "role": dlg.result_role,
        }
        self.dirty = True
        if self._save():
            self._refresh_table()
            self._set_status(f"Added user '{username}' with role '{dlg.result_role}'.")

    def action_edit(self):
        old_username = self._selected_username()
        if not old_username:
            return

        current_role = self.users[old_username].get("role", "user")
        dlg = EditUserDialog(self, existing_username=old_username, current_role=current_role)
        if dlg.result_username is None:
            return  # cancelled

        new_username = dlg.result_username
        if new_username != old_username and new_username in self.users:
            messagebox.showerror("Error", f"User '{new_username}' already exists.")
            return

        info = self.users.pop(old_username)
        info["role"] = dlg.result_role
        if dlg.result_password is not None:
            info["password"] = hash_password(dlg.result_password)
        self.users[new_username] = info
        self.dirty = True

        if self._save():
            self._refresh_table()
            if new_username != old_username:
                self.tree.selection_set(new_username)
            self._set_status(f"Updated user '{new_username}'.")

    def action_delete(self):
        username = self._selected_username()
        if not username:
            return
        if not messagebox.askyesno(
            "Confirm delete", f"Delete user '{username}'? This cannot be undone (though a backup file is kept)."
        ):
            return
        del self.users[username]
        self.dirty = True
        if self._save():
            self._refresh_table()
            self._set_status(f"Deleted user '{username}'.")

    def action_verify(self):
        username = self._selected_username()
        if not username:
            return
        pw = simpledialog.askstring(
            "Verify password", f"Enter password to check against '{username}':", show="*", parent=self
        )
        if pw is None:
            return
        ok = check_password_hash(self.users[username].get("password", ""), pw)
        if ok:
            messagebox.showinfo("Verify password", "✓ Password matches.")
        else:
            messagebox.showerror("Verify password", "✗ Password does not match.")
        self._set_status(f"Verified password for '{username}': {'match' if ok else 'no match'}")

    def on_close(self):
        """Called on window close / File > Quit: make sure nothing unsaved is lost."""
        if self.dirty:
            if self._save():
                self._set_status("Saved on exit.")
            else:
                # Save failed — ask before losing changes
                if not messagebox.askyesno(
                    "Save failed", "Could not save changes. Quit anyway and lose them?"
                ):
                    return
        self.destroy()


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else default_users_path()
    app = UserManagerApp(path)
    app.mainloop()



if __name__ == "__main__":
    main()