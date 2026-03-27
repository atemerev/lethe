#!/usr/bin/env python3
"""Resolve a Telegram username to a numeric user ID using the Client API."""

import argparse
import sys

from telethon.sync import TelegramClient

API_ID = int(input("Enter your API ID: "))
API_HASH = input("Enter your API hash: ")


def main():
    parser = argparse.ArgumentParser(description="Resolve Telegram username to user ID")
    parser.add_argument("username", help="Telegram username (with or without @)")
    args = parser.parse_args()

    username = args.username.lstrip("@")

    with TelegramClient("resolve_session", API_ID, API_HASH) as client:
        entity = client.get_entity(username)
        print(f"Username: @{username}")
        print(f"User ID:  {entity.id}")
        print(f"Name:     {entity.first_name or ''} {entity.last_name or ''}".strip())


if __name__ == "__main__":
    main()
