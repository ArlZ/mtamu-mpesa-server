import os
import re
import json
import base64
import logging
from datetime import datetime
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Daraja Credentials ─────────────────────────────────────────────────────────
CONSUMER_KEY    = os.environ.get("CONSUMER_KEY",    "3WIa0dq1OYyVsADv5XGSgIBCsOfGLHJfuqkoJAB779vE3UUG")
CONSUMER_SECRET = os.environ.get("CONSUMER_SECRET", "wjH4XPnSxZKsSO05hfevEpPSO71LU0Liccm9y3xwRcTrBtY8YrwDizvTINUI8x3D")
SHORTCODE       = os.environ.get("SHORTCODE",       "4574141")
PASSKEY         = os.environ.get("PASSKEY",         "e365eedebcc81a96e1e35b2b03f3d5e03e0b3e6840dec31623eaa218c6671dc6")

# Set this to your Render URL once deployed (e.g. https://mtamu-mpesa.onrender.com)
BASE_URL = os.environ.get("BASE_URL", "https://your-app-name.onrender.com")

DARAJA_AUTH_URL = "https://api.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
DARAJA_STK_URL  = "https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest"

# ── In-memory debug log (last 20 webhooks) ─────────────────────────────────────
recent_webhooks = []


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_access_token():
    credentials = base64.b64encode(f"{CONSUMER_KEY}:{CONSUMER_SECRET}".encode()).decode()
    response = requests.get(
        DARAJA_AUTH_URL,
        headers={"Authorization": f"Basic {credentials}"},
        timeout=30
    )
    response.raise_for_status()
    return response.json()["access_token"]


def format_phone(phone: str) -> str:
    """Normalise any Kenyan phone number to 254XXXXXXXXX."""
    phone = re.sub(r"[^\d]", "", phone)
    if phone.startswith("0") and len(phone) == 10:
        phone = "254" + phone[1:]
    elif not phone.startswith("254"):
        phone = "254" + phone
    return phone


def extract_phone(data: dict):
    """
    Search the JotForm payload for a Kenyan phone number.
    Checks field names containing 'phone', 'mpesa', 'tel', 'mobile' first,
    then falls back to scanning all values.
    """
    phone_re = re.compile(r"((?:\+?254|0)[17]\d{8})")

    priority_keys = [k for k in data if any(t in k.lower() for t in ("phone", "mpesa", "tel", "mobile", "number"))]
    search_order  = priority_keys + [k for k in data if k not in priority_keys]

    for key in search_order:
        val = str(data[key]).strip()
        m = phone_re.search(val)
        if m:
            return m.group(1)
    return None


def extract_amount(data: dict):
    """
    Search the JotForm payload for a payment amount.
    Checks field names containing 'meal', 'amount', 'price', 'total', 'cost',
    'payment', 'order' first, then scans all values.
    Extracts the last integer found in the value (handles 'Chicken - KES 850').
    """
    priority_keys = [k for k in data if any(t in k.lower() for t in
                     ("meal", "amount", "price", "total", "cost", "payment", "order", "item"))]
    search_order  = priority_keys + [k for k in data if k not in priority_keys]

    for key in search_order:
        val = str(data[key]).replace(",", "")
        nums = re.findall(r"\b(\d{2,6})\b", val)  # 2-6 digit numbers = plausible KES
        for n in reversed(nums):                   # take last number in string
            amount = int(n)
            if 10 <= amount <= 70000:              # M-PESA allowed range
                return amount
    return None


def flatten_jotform_payload(raw: dict) -> dict:
    """
    JotForm sometimes nests data inside a 'rawRequest' JSON string.
    Flatten it so our extractors see a simple key→value dict.
    """
    flat = {}
    for k, v in raw.items():
        if k == "rawRequest" and isinstance(v, str):
            try:
                nested = json.loads(v)
                flat.update(nested)
                continue
            except Exception:
                pass
        flat[k] = v
    return flat


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    """Receives JotForm submission and triggers M-PESA STK Push."""
    try:
        # Parse payload (form-encoded or JSON)
        if request.content_type and "json" in request.content_type:
            raw = request.get_json(force=True) or {}
        else:
            raw = request.form.to_dict()
            if not raw:
                try:
                    raw = json.loads(request.get_data(as_text=True))
                except Exception:
                    raw = {}

        data = flatten_jotform_payload(raw)
        logger.info("Webhook received:\n%s", json.dumps(data, indent=2, ensure_ascii=False))

        # Save for /debug
        recent_webhooks.append({"ts": datetime.now().isoformat(), "data": data})
        if len(recent_webhooks) > 20:
            recent_webhooks.pop(0)

        # Extract fields
        phone  = extract_phone(data)
        amount = extract_amount(data)

        if not phone:
            logger.error("Phone not found. Payload: %s", data)
            return jsonify({"error": "Could not find M-PESA phone number in submission", "received_keys": list(data.keys())}), 400
        if not amount:
            logger.error("Amount not found. Payload: %s", data)
            return jsonify({"error": "Could not find payment amount in submission", "received_keys": list(data.keys())}), 400

        phone_fmt = format_phone(phone)
        logger.info("Initiating STK Push → Phone: %s | Amount: KES %s", phone_fmt, amount)

        # Daraja auth
        token = get_access_token()

        # Build STK request
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        password  = base64.b64encode(f"{SHORTCODE}{PASSKEY}{timestamp}".encode()).decode()

        stk_payload = {
            "BusinessShortCode": SHORTCODE,
            "Password":          password,
            "Timestamp":         timestamp,
            "TransactionType":   "CustomerPayBillOnline",
            "Amount":            amount,
            "PartyA":            phone_fmt,
            "PartyB":            SHORTCODE,
            "PhoneNumber":       phone_fmt,
            "CallBackURL":       f"{BASE_URL}/callback",
            "AccountReference":  "MtamuPreorder",
            "TransactionDesc":   "Mtamu Preorder Payment"
        }

        stk_resp = requests.post(
            DARAJA_STK_URL,
            json=stk_payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30
        )
        result = stk_resp.json()
        logger.info("STK Push response: %s", result)

        return jsonify({
            "status":          "STK Push initiated",
            "phone":           phone_fmt,
            "amount":          amount,
            "daraja_response": result
        })

    except Exception as exc:
        logger.exception("Unhandled error in /webhook")
        return jsonify({"error": str(exc)}), 500


@app.route("/callback", methods=["POST"])
def callback():
    """Daraja calls this URL after the customer approves/rejects the payment."""
    data = request.get_json(force=True) or {}
    logger.info("Payment callback received:\n%s", json.dumps(data, indent=2))

    try:
        stk = data["Body"]["stkCallback"]
        code = stk["ResultCode"]
        desc = stk["ResultDesc"]

        if code == 0:
            items = {i["Name"]: i.get("Value") for i in stk["CallbackMetadata"]["Item"]}
            logger.info("✅ Payment SUCCESS: %s", items)
        else:
            logger.warning("❌ Payment FAILED (code %s): %s", code, desc)

    except Exception as exc:
        logger.error("Could not parse callback: %s", exc)

    # Daraja requires this exact response
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


@app.route("/debug", methods=["GET"])
def debug():
    """Returns the last 20 webhook payloads — useful for checking field names."""
    return jsonify({"count": len(recent_webhooks), "webhooks": recent_webhooks})


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "Mtamu M-PESA server is running 🔥", "time": datetime.now().isoformat()})


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
