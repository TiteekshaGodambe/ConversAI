"""
Create the Oracle schema ConversAI loads its tables into (run once, as ADMIN).

  set ORACLE_DSN=localhost:1521/myatp_low.adb.oraclecloud.com
  python tables/oracle_create_user.py                 # creates user CONVERSAI
  python tables/oracle_create_user.py --user MYUSER

Asks for the ADMIN password and the new user's password at hidden prompts (nothing is echoed or
stored). Grants only what loading the tables needs: CREATE SESSION, CREATE TABLE and space on the
data tablespace. Safe to re-run: an existing user is kept and the grants are re-applied.

Autonomous Database password rules: 12-30 characters, upper and lower case letters and a digit,
no double quote ("), and it must not contain the user name or the word "admin".
"""

import argparse
import getpass
import os
import re
import sys

import oracledb


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", default="CONVERSAI", help="schema to create (default CONVERSAI)")
    ap.add_argument("--admin", default="ADMIN", help="administrator user (default ADMIN)")
    a = ap.parse_args()

    dsn = os.environ.get("ORACLE_DSN")
    if not dsn:
        sys.exit("Set ORACLE_DSN first, e.g. set ORACLE_DSN=localhost:1521/myatp_low.adb.oraclecloud.com")
    user = a.user.upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,29}", user):
        sys.exit("User name: letters, digits and _ only, starting with a letter.")

    admin_pw = getpass.getpass(f"{a.admin} password: ")
    new_pw = getpass.getpass(f"New password for {user}: ")
    if new_pw != getpass.getpass(f"Repeat password for {user}: "):
        sys.exit("Passwords do not match.")
    if '"' in new_pw:
        sys.exit('The password may not contain a double quote (").')

    with oracledb.connect(user=a.admin, password=admin_pw, dsn=dsn) as con, con.cursor() as cur:
        cur.execute("SELECT tablespace_name FROM dba_tablespaces WHERE tablespace_name IN ('DATA', 'USERS')")
        spaces = {r[0] for r in cur}
        ts = "DATA" if "DATA" in spaces else "USERS"
        cur.execute("SELECT COUNT(*) FROM dba_users WHERE username = :u", u=user)
        exists = cur.fetchone()[0] > 0
        if exists:
            print(f"User {user} already exists: keeping it (password unchanged), re-applying grants.")
        else:
            cur.execute(f'CREATE USER {user} IDENTIFIED BY "{new_pw}" DEFAULT TABLESPACE {ts} QUOTA UNLIMITED ON {ts}')
            print(f"Created user {user} (tablespace {ts}).")
        cur.execute(f"ALTER USER {user} QUOTA UNLIMITED ON {ts}")
        cur.execute(f"GRANT CREATE SESSION, CREATE TABLE TO {user}")
    print(f"Done. Next:\n  set ORACLE_USER={user}\n  (set ORACLE_PASSWORD to {user}'s password at a hidden prompt)\n"
          "  python tables\\build_tables.py --oracle")


if __name__ == "__main__":
    main()
