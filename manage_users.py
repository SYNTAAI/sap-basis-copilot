#!/usr/bin/env python3
"""Manage SyntaAI MCP login users (the accounts used to sign in during Claude's
OAuth flow at https://mcp2.syntaai.com).

Run from /opt/mcp-server-live (so it picks up the same data dir as the server):

  venv/bin/python manage_users.py add  EMAIL [--name NAME] [--role admin|analyst|viewer] [--password PW]
  venv/bin/python manage_users.py list
  venv/bin/python manage_users.py passwd EMAIL [--password PW]
  venv/bin/python manage_users.py delete EMAIL

If --password is omitted you are prompted (hidden input).
Changes take effect immediately (the server reads users.json per login) — no
restart required, but restarting is harmless: sudo systemctl restart syntaai-mcp-live
"""

import argparse
import getpass
import sys

import user_store


def _get_pw(args) -> str:
    if args.password:
        return args.password
    p1 = getpass.getpass("Password: ")
    p2 = getpass.getpass("Confirm password: ")
    if p1 != p2:
        sys.exit("error: passwords do not match")
    return p1


def main() -> None:
    ap = argparse.ArgumentParser(description="Manage SyntaAI MCP login users")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="register a new user (or overwrite an existing one)")
    a.add_argument("email")
    a.add_argument("--name", default="")
    a.add_argument("--role", default="analyst", choices=user_store.VALID_ROLES)
    a.add_argument("--password")

    p = sub.add_parser("passwd", help="reset a user's password")
    p.add_argument("email")
    p.add_argument("--password")

    d = sub.add_parser("delete", help="remove a user")
    d.add_argument("email")

    sub.add_parser("list", help="list registered users")

    args = ap.parse_args()

    if args.cmd == "add":
        existed = user_store.add_user(args.email, _get_pw(args), args.name, args.role)
        verb = "updated" if existed else "registered"
        print(f"{verb}: {args.email.strip().lower()}  (role={args.role})")
    elif args.cmd == "passwd":
        try:
            user_store.set_password(args.email, _get_pw(args))
            print(f"password updated for {args.email.strip().lower()}")
        except KeyError:
            sys.exit(f"error: no such user: {args.email}")
    elif args.cmd == "delete":
        print("deleted" if user_store.delete_user(args.email) else "not found")
    elif args.cmd == "list":
        users = user_store.list_users()
        if not users:
            print("(no users registered — add one with: manage_users.py add EMAIL --role admin)")
            return
        for e, info in users.items():
            print(f"  {e:<35} role={info.get('role','?'):<8} name={info.get('name','')}")


if __name__ == "__main__":
    main()
