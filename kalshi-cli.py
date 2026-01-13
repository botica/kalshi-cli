#!/usr/bin/env python3
"""
Kalshi CLI - WebSocket contract data streamer
Usage: python kalshi-cli.py reader <contract-id>
"""

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import websockets
except ImportError:
    print("Error: websockets package required. Install with: pip install websockets")
    sys.exit(1)

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except ImportError:
    print("Error: cryptography package required. Install with: pip install cryptography")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Error: requests package required. Install with: pip install requests")
    sys.exit(1)


KALSHI_API_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
CONFIG_FILE = Path.home() / ".kalshi-cli.json"

SPINNER_FRAMES = ['|', '/', '-', '\\']


def get_spinner_frame(index):
    """Get spinner character for given index."""
    return SPINNER_FRAMES[index % len(SPINNER_FRAMES)]


def print_spinner(frame):
    """Update spinner line, cursor stays on line below."""
    sys.stdout.write(f"\033[A\r  Press Ctrl+C to exit {frame}\n")
    sys.stdout.flush()


def load_config():
    """Load config from file if it exists."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def save_config(api_key: str, private_key: str):
    """Save config to file."""
    config = {
        "api_key": api_key,
        "private_key": private_key
    }
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def get_credentials():
    """Get credentials from config or prompt user."""
    config = load_config()

    if config and config.get("api_key") and config.get("private_key"):
        return config["api_key"], config["private_key"]

    print("Enter your Kalshi API credentials:")
    api_key = input("API Key: ").strip()

    print("Paste your private key (including BEGIN/END lines):")
    lines = []
    while True:
        line = input()
        lines.append(line)
        if "END" in line:
            break
    private_key = "\n".join(lines)

    if not api_key or not private_key:
        print("Error: API key and private key are required")
        sys.exit(1)

    save_config(api_key, private_key)
    return api_key, private_key


def sign_request(private_key_pem: str, timestamp: str, method: str, path: str) -> str:
    """Sign request with RSA private key using PSS padding."""
    message = f"{timestamp}{method}{path}"

    # Load the private key
    private_key = serialization.load_pem_private_key(
        private_key_pem.encode(),
        password=None
    )

    # Sign with RSA-PSS (what Kalshi uses)
    signature = private_key.sign(
        message.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256()
    )

    return base64.b64encode(signature).decode()


class ContractReader:
    def __init__(self, contract_id: str, api_key: str, api_secret: str):
        self.contract_id = contract_id
        self.api_key = api_key
        self.api_secret = api_secret
        self.current_data = {}
        self.spinner_index = 0

    def fetch_initial_data(self):
        """Fetch initial market data via REST API."""
        path = f"/markets/{self.contract_id}"
        timestamp = str(int(time.time() * 1000))
        signature = sign_request(self.api_secret, timestamp, "GET", f"/trade-api/v2{path}")

        headers = {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

        try:
            resp = requests.get(f"{KALSHI_API_URL}{path}", headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json().get("market", {})
                self.current_data = {
                    "title": data.get("title"),
                    "subtitle": data.get("subtitle"),
                    "status": data.get("status"),
                    "yes_bid": data.get("yes_bid"),
                    "yes_ask": data.get("yes_ask"),
                    "no_bid": data.get("no_bid"),
                    "no_ask": data.get("no_ask"),
                    "last_price": data.get("last_price"),
                    "volume": data.get("volume"),
                    "open_interest": data.get("open_interest"),
                }
        except Exception:
            pass

    def clear_screen(self):
        os.system('cls' if os.name == 'nt' else 'clear')

    def format_price(self, price: int | None) -> str:
        if price is None:
            return "N/A"
        return f"${price / 100:.2f}"

    def display_contract(self):
        self.clear_screen()

        print("=" * 60)
        print(f"  CONTRACT: {self.contract_id}")
        print("=" * 60)

        if not self.current_data:
            print("\n  Waiting for data...\n")
        else:
            data = self.current_data

            print(f"\n  Title: {data.get('title', 'N/A')}")
            print(f"  Status: {data.get('status', 'N/A')}")

            print("\n  --- Pricing ---")
            print(f"  Yes Bid:  {self.format_price(data.get('yes_bid'))}")
            print(f"  Yes Ask:  {self.format_price(data.get('yes_ask'))}")
            print(f"  No Bid:   {self.format_price(data.get('no_bid'))}")
            print(f"  No Ask:   {self.format_price(data.get('no_ask'))}")

            if 'last_price' in data:
                print(f"\n  Last Price: {self.format_price(data.get('last_price'))}")

            if 'volume' in data:
                print(f"  Volume: {data.get('volume', 0):,}")

            if 'open_interest' in data:
                print(f"  Open Interest: {data.get('open_interest', 0):,}")

        print("\n" + "=" * 60)
        print(f"  Last Update: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)
        print(f"  Press Ctrl+C to exit {get_spinner_frame(self.spinner_index)}")

    def update_from_message(self, msg: dict):
        msg_type = msg.get("type")

        if msg_type == "orderbook_snapshot":
            data = msg.get("msg", {})
            if data.get("market_ticker") == self.contract_id:
                yes_bids = data.get("yes", [])
                no_bids = data.get("no", [])
                if yes_bids:
                    self.current_data["yes_bid"] = yes_bids[0][0] if yes_bids else None
                if no_bids:
                    self.current_data["no_bid"] = no_bids[0][0] if no_bids else None

        elif msg_type == "orderbook_delta":
            data = msg.get("msg", {})
            if data.get("market_ticker") == self.contract_id:
                if "price" in data:
                    side = data.get("side")
                    if side == "yes":
                        self.current_data["yes_bid"] = data.get("price")
                    elif side == "no":
                        self.current_data["no_bid"] = data.get("price")

        elif msg_type == "ticker":
            data = msg.get("msg", {})
            if data.get("market_ticker") == self.contract_id:
                # Only update fields that are actually present in the message
                for key in ["yes_bid", "yes_ask", "no_bid", "no_ask", "last_price", "volume", "open_interest"]:
                    if key in data:
                        self.current_data[key] = data[key]

        elif msg_type == "trade":
            data = msg.get("msg", {})
            if data.get("market_ticker") == self.contract_id:
                self.current_data["last_price"] = data.get("yes_price")
                self.current_data["volume"] = data.get("volume", self.current_data.get("volume", 0))

        elif msg_type == "market":
            data = msg.get("msg", {})
            if data.get("ticker") == self.contract_id:
                # Only update fields that are actually present in the message
                for key in ["title", "status", "yes_bid", "yes_ask", "volume", "open_interest"]:
                    if key in data:
                        self.current_data[key] = data[key]

    async def subscribe(self, ws):
        await ws.send(json.dumps({
            "id": 1,
            "cmd": "subscribe",
            "params": {
                "channels": ["ticker"],
                "market_tickers": [self.contract_id]
            }
        }))
        await ws.send(json.dumps({
            "id": 2,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": [self.contract_id]
            }
        }))
        await ws.send(json.dumps({
            "id": 3,
            "cmd": "subscribe",
            "params": {
                "channels": ["trade"],
                "market_tickers": [self.contract_id]
            }
        }))

    async def run(self):
        self.fetch_initial_data()
        self.display_contract()

        while True:
            try:
                # Generate auth headers with RSA signature
                timestamp = str(int(time.time() * 1000))
                path = "/trade-api/ws/v2"

                try:
                    signature = sign_request(self.api_secret, timestamp, "GET", path)
                except Exception as e:
                    print(f"Error signing request: {e}")
                    print("Make sure your private key is valid PEM format")
                    sys.exit(1)

                headers = {
                    "KALSHI-ACCESS-KEY": self.api_key,
                    "KALSHI-ACCESS-SIGNATURE": signature,
                    "KALSHI-ACCESS-TIMESTAMP": timestamp,
                }

                async with websockets.connect(
                    KALSHI_WS_URL,
                    additional_headers=headers
                ) as ws:
                    await self.subscribe(ws)

                    while True:
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=0.2)
                            msg = json.loads(message)
                            old_data = dict(self.current_data)
                            self.update_from_message(msg)
                            if self.current_data != old_data:
                                self.display_contract()
                        except asyncio.TimeoutError:
                            pass
                        except json.JSONDecodeError:
                            continue
                        # Update spinner on every iteration
                        self.spinner_index += 1
                        print_spinner(get_spinner_frame(self.spinner_index))

            except websockets.exceptions.ConnectionClosed:
                print("\nConnection closed. Reconnecting in 5 seconds...")
                await asyncio.sleep(5)
            except Exception as e:
                print(f"\nError: {e}. Reconnecting in 5 seconds...")
                await asyncio.sleep(5)


def main():
    parser = argparse.ArgumentParser(
        description="Kalshi CLI - Stream contract data via WebSocket"
    )
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    reader_parser = subparsers.add_parser(
        "reader",
        help="Stream real-time contract data"
    )
    reader_parser.add_argument(
        "contract_id",
        help="The contract/market ticker ID"
    )

    args = parser.parse_args()

    if args.command == "reader":
        api_key, private_key = get_credentials()
        reader = ContractReader(args.contract_id.upper(), api_key, private_key)
        try:
            asyncio.run(reader.run())
        except KeyboardInterrupt:
            print("\n\nExiting...")
            sys.exit(0)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
