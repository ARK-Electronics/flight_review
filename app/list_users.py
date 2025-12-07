#! /usr/bin/env python3

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
    db_file = get_db_filename()
    
    if not os.path.exists(db_file):
        print(f"Error: Database file {db_file} not found.")
        sys.exit(1)
        
    con = sqlite3.connect(db_file)
    cur = con.cursor()
    
    try:
        cur.execute("SELECT Username, Email, IsAdmin FROM Users")
        users = cur.fetchall()
        
        if not users:
            print("No users found.")
        else:
            print(f"{'Username':<20} | {'Email':<30} | {'Is Admin':<10}")
            print("-" * 66)
            for user in users:
                username = user[0]
                email = user[1] if user[1] else ""
                is_admin = "Yes" if user[2] else "No"
                print(f"{username:<20} | {email:<30} | {is_admin:<10}")
                
    except sqlite3.OperationalError as e:
        print(f"Error querying database: {e}")
        print("Make sure the database schema is up to date.")
    finally:
        con.close()

if __name__ == '__main__':
    main()
