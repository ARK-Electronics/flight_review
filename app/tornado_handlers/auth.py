import sqlite3
import os
import sys
import time
import traceback
from passlib.hash import bcrypt
import tornado.web
from tornado.ioloop import IOLoop
import uuid
from .send_email import send_approval_email, send_account_approved_email, send_reset_password_email
from .upload import process_pending_logs_for_user
from config import get_domain_name, get_http_protocol

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
                        # Parse any logs that were deferred while this account
                        # looked unapproved (or was still pending). Safe no-op
                        # when there are none.
                        IOLoop.current().run_in_executor(
                            None, process_pending_logs_for_user, username)
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
            is_admin = 1 if count == 0 else 0
            
            account_token = str(uuid.uuid4())

            cur.execute("INSERT INTO Users (Username, PasswordHash, Email, Approved, AccountToken, IsAdmin) VALUES (?, ?, ?, ?, ?, ?)",
                        (username, password_hash, email, approved, account_token, is_admin))
            con.commit()
            
            msg = "Registration successful. "
            if approved:
                msg += "You can now login."
            else:
                # Send approval email to admin
                protocol = get_http_protocol()
                domain = get_domain_name()
                approve_url = f"{protocol}://{domain}/approve_user?token={account_token}"
                
                admin_email = "logs@arkelectron.com"
                if send_approval_email(admin_email, username, email, approve_url):
                    msg += "Your account is pending approval. An administrator has been notified."
                else:
                    msg += "Registration successful, but failed to notify administrator. Please contact support."
                
            self.render_jinja('register.html', error=None, message=msg)
            
        except Exception as e:
            print("Error during registration:", flush=True)
            traceback.print_exc()
            self.render_jinja('register.html', error=f"Error: {str(e)}", message=None)
        finally:
            if con:
                con.close()

class ApproveUserHandler(TornadoRequestHandlerBase):
    def get(self):
        token = self.get_argument("token", None)
        if not token:
            self.render_jinja('login.html', error="Invalid approval link.", next="/")
            return

        con = None
        try:
            con = sqlite3.connect(get_db_filename())
            cur = con.cursor()
            
            # Find user with this token
            cur.execute("SELECT Username, Approved, Email FROM Users WHERE AccountToken=?", (token,))
            row = cur.fetchone()
            
            if row:
                username = row[0]
                approved = row[1]
                email = row[2]
                
                if approved:
                    self.render_jinja('login.html', error=None, message=f"Account for {username} already approved.", next="/")
                else:
                    # Approve the account
                    cur.execute("UPDATE Users SET Approved=1 WHERE Username=?", (username,))
                    con.commit()
                    
                    # Send approval notification to user
                    protocol = get_http_protocol()
                    domain = get_domain_name()
                    login_url = f"{protocol}://{domain}/login"
                    send_account_approved_email(email, username, login_url)

                    # Parse any logs the user uploaded while pending approval
                    IOLoop.current().run_in_executor(
                        None, process_pending_logs_for_user, username)

                    self.render_jinja('login.html', error=None, message=f"Account for {username} approved successfully!", next="/")
            else:
                self.render_jinja('login.html', error="Invalid or expired approval link.", next="/")
                
        except Exception:
            traceback.print_exc()
            self.write_error(500)
        finally:
            if con:
                con.close()


class ForgotPasswordHandler(TornadoRequestHandlerBase):
    def get(self):
        self.render_jinja('forgot_password.html', error=None, message=None)

    def post(self):
        con = None
        try:
            email = self.get_argument("email")
            con = sqlite3.connect(get_db_filename())
            cur = con.cursor()
            cur.execute("SELECT Username FROM Users WHERE Email=?", (email,))
            row = cur.fetchone()
            
            if row:
                username = row[0]
                token = str(uuid.uuid4())
                # Expiration 1 hour from now
                expiration = time.time() + 3600 
                
                cur.execute("UPDATE Users SET ResetToken=?, ResetTokenExpiration=? WHERE Username=?", (token, expiration, username))
                con.commit()
                
                protocol = get_http_protocol()
                domain = get_domain_name()
                reset_url = f"{protocol}://{domain}/reset_password?token={token}"
                
                if send_reset_password_email(email, username, reset_url):
                    self.render_jinja('forgot_password.html', error=None, message="If an account with that email exists, a password reset link has been sent.")
                else:
                    self.render_jinja('forgot_password.html', error="Failed to send email.", message=None)
            else:
                # Don't reveal if email exists
                self.render_jinja('forgot_password.html', error=None, message="If an account with that email exists, a password reset link has been sent.")
        except Exception:
            traceback.print_exc()
            self.write_error(500)
        finally:
            if con:
                con.close()

class ResetPasswordHandler(TornadoRequestHandlerBase):
    def get(self):
        con = None
        try:
            token = self.get_argument("token", None)
            if not token:
                self.redirect("/login")
                return
                
            con = sqlite3.connect(get_db_filename())
            cur = con.cursor()
            cur.execute("SELECT Username, ResetTokenExpiration FROM Users WHERE ResetToken=?", (token,))
            row = cur.fetchone()
            
            if row:
                expiration = row[1]
                if time.time() < expiration:
                    self.render_jinja('reset_password.html', error=None, message=None, token=token)
                else:
                    self.render_jinja('login.html', error="Password reset link has expired.", next="/")
            else:
                self.render_jinja('login.html', error="Invalid password reset link.", next="/")
        except Exception:
            traceback.print_exc()
            self.write_error(500)
        finally:
            if con:
                con.close()

    def post(self):
        con = None
        try:
            token = self.get_argument("token")
            password = self.get_argument("password")
            
            con = sqlite3.connect(get_db_filename())
            cur = con.cursor()
            cur.execute("SELECT Username, ResetTokenExpiration FROM Users WHERE ResetToken=?", (token,))
            row = cur.fetchone()
            
            if row:
                username = row[0]
                expiration = row[1]
                if time.time() < expiration:
                    password_hash = bcrypt.hash(password)
                    cur.execute("UPDATE Users SET PasswordHash=?, ResetToken='', ResetTokenExpiration=0 WHERE Username=?", (password_hash, username))
                    con.commit()
                    self.render_jinja('login.html', error=None, message="Password reset successful. You can now login.", next="/")
                else:
                    self.render_jinja('login.html', error="Password reset link has expired.", next="/")
            else:
                self.render_jinja('login.html', error="Invalid password reset link.", next="/")
        except Exception:
            traceback.print_exc()
            self.write_error(500)
        finally:
            if con:
                con.close()
