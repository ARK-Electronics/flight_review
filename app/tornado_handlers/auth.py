"""Authentication handlers: login, logout, register, approve, password reset."""

import os
import sqlite3
import sys
import time
import traceback
import uuid

from passlib.hash import bcrypt
from tornado.ioloop import IOLoop
import tornado.web

# this is needed for the following imports
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)),
                             '../plot_app'))
from config import get_db_filename, get_domain_name, get_http_protocol

#pylint: disable=relative-beyond-top-level
from .send_email import (
    send_approval_email, send_account_approved_email, send_reset_password_email)
from .upload import process_pending_logs_for_user
from .common import TornadoRequestHandlerBase
from .security import SESSION_MAX_AGE_DAYS, client_ip, get_rate_limiter, session_cookie_value


def _safe_next_url(value):
    """Keep browser redirects local, including browsers' backslash normalization."""
    if (value.startswith('/') and not value.startswith('//')
            and '\\' not in value and not any(ord(char) < 32 for char in value)):
        return value
    return '/'


class AuthRequestHandler(TornadoRequestHandlerBase):
    """Limit public credential operations before expensive password hashing."""

    rate_limit = 10
    rate_window = 60

    def prepare(self):
        """Throttle public account mutations before running their handlers."""
        super().prepare()
        if self.request.method == 'POST':
            limiter = get_rate_limiter()
            limiter.prune()
            if not limiter.check(type(self).__name__, client_ip(self),
                                 self.rate_limit, self.rate_window):
                raise tornado.web.HTTPError(429, 'Too many attempts. Try again later.')

    def write_error(self, status_code, **kwargs):
        """Include the retry window on rejected authentication attempts."""
        if status_code == 429:
            self.set_header('Retry-After', str(self.rate_window))
        super().write_error(status_code, **kwargs)


class LoginHandler(AuthRequestHandler):
    """Handle user login form GET/POST."""

    def get(self):
        """Render the login page."""
        self.render_jinja('login.html', error=None,
                          next=self.get_argument("next", "/"))

    def post(self):
        """Authenticate credentials and set the session cookie."""
        try:
            username = self.get_argument("username")
            password = self.get_argument("password")
            next_url = self.get_argument("next", "/")

            con = sqlite3.connect(get_db_filename())
            cur = con.cursor()
            cur.execute(
                "SELECT PasswordHash, Approved FROM Users WHERE Username=?",
                (username,))
            row = cur.fetchone()
            con.close()

            if row:
                password_hash = row[0]
                approved = row[1]
                if bcrypt.verify(password, password_hash):
                    if approved:
                        self.set_secure_cookie(
                            "user", session_cookie_value(
                                username, password_hash, self.settings['cookie_secret']),
                            secure=get_http_protocol() == 'https',
                            httponly=True, samesite='Lax', expires_days=SESSION_MAX_AGE_DAYS)
                        # Parse any logs that were deferred while this account
                        # looked unapproved (or was still pending). Safe no-op
                        # when there are none.
                        IOLoop.current().run_in_executor(
                            None, process_pending_logs_for_user, username)
                        self.redirect(_safe_next_url(next_url))
                        return
                    self.render_jinja(
                        'login.html',
                        error="Account pending approval.",
                        next=next_url)
                    return

            self.render_jinja(
                'login.html',
                error="Invalid username or password",
                next=next_url)
        except Exception:
            traceback.print_exc()
            self.write_error(500)


class LogoutHandler(TornadoRequestHandlerBase):
    """Clear the session cookie and redirect to login."""

    def get(self):
        """Log the user out."""
        self.clear_cookie("user")
        self.redirect("/login")


class RegisterHandler(AuthRequestHandler):
    """Handle new user registration."""

    rate_limit = 5
    rate_window = 3600

    def get(self):
        """Render the registration page."""
        self.render_jinja('register.html', error=None, message=None)

    def post(self):
        """Create an unapproved account; administrators are provisioned locally."""
        con = None
        try:
            username = self.get_argument("username")
            password = self.get_argument("password")
            email = self.get_argument("email")

            print(f"Registering user: {username}, email: {email}", flush=True)

            con = sqlite3.connect(get_db_filename())
            cur = con.cursor()

            # Check if user exists
            cur.execute(
                "SELECT Username FROM Users WHERE Username=? "
                "UNION SELECT Username FROM DeletedUsers WHERE Username=?",
                (username, username))
            if cur.fetchone():
                self.render_jinja(
                    'register.html',
                    error="Username is unavailable",
                    message=None)
                return

            # Public signup must never grant administrator privileges, including
            # on a new database. Operators bootstrap with app/set_admin.py.
            password_hash = bcrypt.hash(password)
            account_token = str(uuid.uuid4())

            cur.execute(
                "INSERT INTO Users (Username, PasswordHash, Email, Approved, "
                "AccountToken, IsAdmin) VALUES (?, ?, ?, 0, ?, 0)",
                (username, password_hash, email, account_token))
            con.commit()

            msg = "Registration successful. "
            # Send approval email to admin.
            protocol = get_http_protocol()
            domain = get_domain_name()
            approve_url = (
                f"{protocol}://{domain}/approve_user?token={account_token}")

            admin_email = "logs@arkelectron.com"
            if send_approval_email(
                    admin_email, username, email, approve_url):
                msg += ("Your account is pending approval. "
                        "An administrator has been notified.")
            else:
                msg += ("Failed to notify the administrator. "
                        "Please contact support for account approval.")

            self.render_jinja('register.html', error=None, message=msg)

        except Exception:
            print("Error during registration:", flush=True)
            traceback.print_exc()
            self.render_jinja(
                'register.html', error="Registration failed. Please try again later.",
                message=None)
        finally:
            if con:
                con.close()


class ApproveUserHandler(TornadoRequestHandlerBase):
    """Approve a user account via tokenized email link."""

    def get(self):
        """Approve the account associated with the token query param."""
        token = self.get_argument("token", None)
        if not token:
            self.render_jinja(
                'login.html', error="Invalid approval link.", next="/")
            return

        con = None
        try:
            con = sqlite3.connect(get_db_filename())
            cur = con.cursor()

            # Find user with this token
            cur.execute(
                "SELECT Username, Approved, Email FROM Users "
                "WHERE AccountToken=?",
                (token,))
            row = cur.fetchone()

            if row:
                username = row[0]
                approved = row[1]
                email = row[2]

                if approved:
                    self.render_jinja(
                        'login.html', error=None,
                        message=f"Account for {username} already approved.",
                        next="/")
                else:
                    # Approve the account
                    cur.execute(
                        "UPDATE Users SET Approved=1 WHERE Username=?",
                        (username,))
                    con.commit()

                    # Send approval notification to user
                    protocol = get_http_protocol()
                    domain = get_domain_name()
                    login_url = f"{protocol}://{domain}/login"
                    send_account_approved_email(email, username, login_url)

                    # Parse any logs the user uploaded while pending approval
                    IOLoop.current().run_in_executor(
                        None, process_pending_logs_for_user, username)

                    self.render_jinja(
                        'login.html', error=None,
                        message=(
                            f"Account for {username} approved successfully!"),
                        next="/")
            else:
                self.render_jinja(
                    'login.html',
                    error="Invalid or expired approval link.",
                    next="/")

        except Exception:
            traceback.print_exc()
            self.write_error(500)
        finally:
            if con:
                con.close()


class ForgotPasswordHandler(AuthRequestHandler):
    """Request a password reset email."""

    rate_limit = 5
    rate_window = 3600

    def get(self):
        """Render the forgot-password form."""
        self.render_jinja('forgot_password.html', error=None, message=None)

    def post(self):
        """Send a reset link if the email matches an account."""
        con = None
        try:
            email = self.get_argument("email")
            con = sqlite3.connect(get_db_filename())
            cur = con.cursor()
            cur.execute(
                "SELECT Username FROM Users WHERE Email=?", (email,))
            row = cur.fetchone()

            generic_msg = (
                "If an account with that email exists, "
                "a password reset link has been sent.")

            if row:
                username = row[0]
                token = str(uuid.uuid4())
                # Expiration 1 hour from now
                expiration = time.time() + 3600

                cur.execute(
                    "UPDATE Users SET ResetToken=?, "
                    "ResetTokenExpiration=? WHERE Username=?",
                    (token, expiration, username))
                con.commit()

                protocol = get_http_protocol()
                domain = get_domain_name()
                reset_url = (
                    f"{protocol}://{domain}/reset_password?token={token}")

                send_reset_password_email(email, username, reset_url)
                self.render_jinja(
                    'forgot_password.html', error=None, message=generic_msg)
            else:
                # Don't reveal if email exists
                self.render_jinja(
                    'forgot_password.html',
                    error=None, message=generic_msg)
        except Exception:
            traceback.print_exc()
            self.write_error(500)
        finally:
            if con:
                con.close()


class ResetPasswordHandler(AuthRequestHandler):
    """Set a new password using a reset token."""

    def get(self):
        """Render the reset form if the token is valid."""
        con = None
        try:
            token = self.get_argument("token", None)
            if not token:
                self.redirect("/login")
                return

            con = sqlite3.connect(get_db_filename())
            cur = con.cursor()
            cur.execute(
                "SELECT Username, ResetTokenExpiration FROM Users "
                "WHERE ResetToken=?",
                (token,))
            row = cur.fetchone()

            if row:
                expiration = row[1]
                if time.time() < expiration:
                    self.render_jinja(
                        'reset_password.html',
                        error=None, message=None, token=token)
                else:
                    self.render_jinja(
                        'login.html',
                        error="Password reset link has expired.",
                        next="/")
            else:
                self.render_jinja(
                    'login.html',
                    error="Invalid password reset link.",
                    next="/")
        except Exception:
            traceback.print_exc()
            self.write_error(500)
        finally:
            if con:
                con.close()

    def post(self):
        """Apply the new password when the token is still valid."""
        con = None
        try:
            token = self.get_argument("token")
            password = self.get_argument("password")

            con = sqlite3.connect(get_db_filename())
            cur = con.cursor()
            cur.execute(
                "SELECT Username, ResetTokenExpiration FROM Users "
                "WHERE ResetToken=?",
                (token,))
            row = cur.fetchone()

            if row:
                username = row[0]
                expiration = row[1]
                if time.time() < expiration:
                    password_hash = bcrypt.hash(password)
                    cur.execute(
                        "UPDATE Users SET PasswordHash=?, ResetToken='', "
                        "ResetTokenExpiration=0, ApiKeyHash='', ApiKeyPrefix='', "
                        "ApiKeyCreated=0 WHERE Username=? AND ResetToken=? "
                        "AND ResetTokenExpiration>?",
                        (password_hash, username, token, time.time()))
                    con.commit()
                    if cur.rowcount != 1:
                        self.render_jinja(
                            'login.html', error="Invalid or expired password reset link.",
                            next="/")
                        return
                    self.clear_cookie('user')
                    self.render_jinja(
                        'login.html', error=None,
                        message=("Password reset successful. "
                                 "You can now login."),
                        next="/")
                else:
                    self.render_jinja(
                        'login.html',
                        error="Password reset link has expired.",
                        next="/")
            else:
                self.render_jinja(
                    'login.html',
                    error="Invalid password reset link.",
                    next="/")
        except Exception:
            traceback.print_exc()
            self.write_error(500)
        finally:
            if con:
                con.close()
