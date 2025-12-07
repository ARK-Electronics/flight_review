#! /usr/bin/env python3

import argparse
import sqlite3
import sys
import os

# Add plot_app to path to allow imports
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'plot_app'))

try:
    from plot_app.config import get_db_filename
except ImportError:
    # Fallback if plot_app is not a package in path but config is directly available
    from config import get_db_filename

def main():
    parser = argparse.ArgumentParser(description='Manually set a user as admin.')
    parser.add_argument('username', help='Username to update')
    parser.add_argument('--unset', action='store_true', help='Unset admin privileges (demote to normal user)')
    
    args = parser.parse_args()
    
    username = args.username
    is_admin = 0 if args.unset else 1
    
    db_file = get_db_filename()
    print(f"Using database: {db_file}")
    
    if not os.path.exists(db_file):
        print(f"Error: Database file {db_file} not found.")
        sys.exit(1)
        
    con = sqlite3.connect(db_file)
    cur = con.cursor()
    
    # Check if user exists
    cur.execute("SELECT Username, IsAdmin FROM Users WHERE Username=?", (username,))
    row = cur.fetchone()
    
    if row is None:
        print(f"Error: User '{username}' not found.")
        con.close()
        sys.exit(1)
        
    current_admin_status = row[1]
    
    if current_admin_status == is_admin:
        status_str = "Admin" if is_admin else "User"
        print(f"User '{username}' is already {status_str}.")
    else:
        cur.execute("UPDATE Users SET IsAdmin=? WHERE Username=?", (is_admin, username))
        con.commit()
        action = "promoted to Admin" if is_admin else "demoted to User"
        print(f"User '{username}' has been {action}.")
        
    con.close()

if __name__ == '__main__':
    main()
