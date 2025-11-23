
import sqlite3
import os
import sys
import traceback
from passlib.hash import bcrypt
import tornado.web

# this is needed for the following imports
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), '../plot_app'))
from config import get_db_filename
from .common import TornadoRequestHandlerBase, get_jinja_env

class LoginHandler(TornadoRequestHandlerBase):
    def get(self):
        self.render_jinja('login.html', error=None, next=self.get_argument("next", "/"))

    def post(self):
        try:
            username = self.get_argument("username")
            password = self.get_argument("password")
            next_url = self.get_argument("next", "/")

            con = sqlite3.connect(get_db_filename())
            cur = con.cursor()
            cur.execute("SELECT PasswordHash, Approved FROM Users WHERE Username=?", (username,))
            row = cur.fetchone()
            con.close()

            if row:
                password_hash = row[0]
                approved = row[1]
                if bcrypt.verify(password, password_hash):
                    if approved:
                        self.set_secure_cookie("user", username)
                        self.redirect(next_url)
                        return
                    else:
                        self.render_jinja('login.html', error="Account pending approval.", next=next_url)
                        return

            self.render_jinja('login.html', error="Invalid username or password", next=next_url)
        except Exception:
            traceback.print_exc()
            self.write_error(500)

class LogoutHandler(TornadoRequestHandlerBase):
    def get(self):
        self.clear_cookie("user")
        self.redirect("/login")

class RegisterHandler(TornadoRequestHandlerBase):
    def get(self):
        self.render_jinja('register.html', error=None, message=None)

    def post(self):
        con = None
        try:
            username = self.get_argument("username")
            password = self.get_argument("password")
            email = self.get_argument("email")

            print(f"Registering user: {username}, email: {email}", flush=True)

            password_hash = bcrypt.hash(password)

            con = sqlite3.connect(get_db_filename())
            cur = con.cursor()
            
            # Check if user exists
            cur.execute("SELECT Username FROM Users WHERE Username=?", (username,))
            if cur.fetchone():
                self.render_jinja('register.html', error="Username already exists", message=None)
                return

            # Auto-approve the first user (admin)
            cur.execute("SELECT COUNT(*) FROM Users")
            count = cur.fetchone()[0]
            approved = 1 if count == 0 else 0

            cur.execute("INSERT INTO Users (Username, PasswordHash, Email, Approved) VALUES (?, ?, ?, ?)",
                        (username, password_hash, email, approved))
            con.commit()
            
            msg = "Registration successful. "
            if approved:
                msg += "You can now login."
            else:
                msg += "Please wait for an administrator to approve your account."
                
            self.render_jinja('register.html', error=None, message=msg)
            
        except Exception as e:
            print("Error during registration:", flush=True)
            traceback.print_exc()
            self.render_jinja('register.html', error=f"Error: {str(e)}", message=None)
        finally:
            if con:
                con.close()
