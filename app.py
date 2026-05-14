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
BASE_URL        = os.environ.get("BASE_URL",        "https://mtamu-mpesa-server.onrender.com")

# ── Google Sheets Webhook (Apps Script Web App URL) ────────────────────────────
# Paste your deployed Apps Script URL here after following the setup steps
SHEETS_WEBHOOK_URL = os.environ.get("SHEETS_WEBHOOK_URL", "https://script.google.com/macros/s/AKfycbxFhSZQUjgvq_cy2bynHBjAHjFdcREGjJGMGVUnVuBx40AiuR4fgkGWnDFbqHBjatR2/exec")

DARAJA_AUTH_URL = "https://api.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
DARAJA_STK_URL  = "https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest"

# ── In-memory stores ───────────────────────────────────────────────────────────
# Maps CheckoutRequestID → order details captured from JotForm webhook
pending_payments = {}

# Last 20 webhook payloads for debugging
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


def extract_first_name(data: dict) -> str:
    """Extract first name — checks for JotForm's [first] suffix pattern first."""
    for key, value in data.items():
        k = key.lower()
        if "first" in k or k.endswith("[first]"):
            val = str(value).strip()
            if val:
                return val
    # Fallback: first word of the combined name field
    full = extract_name(data)
    return full.split()[0] if full and " " in full else full


def extract_last_name(data: dict) -> str:
    """Extract last name — checks for JotForm's [last] suffix pattern first."""
    for key, value in data.items():
        k = key.lower()
        if "last" in k or "surname" in k or "lname" in k or k.endswith("[last]"):
            val = str(value).strip()
            if val:
                return val
    # Fallback: everything after the first word
    full = extract_name(data)
    parts = full.split()
    return " ".join(parts[1:]) if len(parts) > 1 else "—"


def extract_name(data: dict) -> str:
    for key, value in data.items():
        if any(t in key.lower() for t in ("name", "fullname", "full_name", "customer")):
            val = str(value).strip()
            if val:
                return val
    return "Unknown"


def extract_pickup_time(data: dict) -> str:
    """Extract pickup/collection time from the form submission."""
    for key, value in data.items():
        if any(t in key.lower() for t in ("pickup", "collection", "collect", "slot", "schedule", "time", "when", "hour")):
            val = str(value).strip()
            if val and val not in ("{}", "None", ""):
                return val
    return "—"


def extract_phone(data: dict):
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
    priority_keys = [k for k in data if any(t in k.lower() for t in
                     ("meal", "amount", "price", "total", "cost", "payment", "order", "item"))]
    search_order  = priority_keys + [k for k in data if k not in priority_keys]
    for key in search_order:
        val = str(data[key]).replace(",", "")
        nums = re.findall(r"\b(\d{2,6})\b", val)
        for n in reversed(nums):
            amount = int(n)
            if 10 <= amount <= 70000:
                return amount
    return None


def extract_meal(data: dict) -> str:
    """Return the raw meal/order field value for logging in the Sheet."""
    for key, value in data.items():
        if any(t in key.lower() for t in ("meal", "order", "item", "dish", "food")):
            val = str(value).strip()
            if val:
                return val
    return "—"


def flatten_jotform_payload(raw: dict) -> dict:
    flat = {}
    for k, v in raw.items():
        if k == "rawRequest" and isinstance(v, str):
            try:
                flat.update(json.loads(v))
                continue
            except Exception:
                pass
        flat[k] = v
    return flat


def post_to_sheet(order: dict):
    """POST confirmed order data to the Google Apps Script webhook."""
    if not SHEETS_WEBHOOK_URL or SHEETS_WEBHOOK_URL == "PASTE_YOUR_APPS_SCRIPT_URL_HERE":
        logger.warning("SHEETS_WEBHOOK_URL not configured — skipping Sheet update.")
        return

    try:
        resp = requests.post(SHEETS_WEBHOOK_URL, json=order, timeout=15)
        logger.info("Sheet update response: %s", resp.text)
    except Exception as exc:
        logger.error("Failed to post to Sheet: %s", exc)


def nairobi_now() -> str:
    """Return current Nairobi time as a readable string."""
    from datetime import timezone, timedelta
    eat = timezone(timedelta(hours=3))
    return datetime.now(eat).strftime("%Y-%m-%d %H:%M:%S")


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    """Receives JotForm submission → triggers M-PESA STK Push → stores order in memory."""
    try:
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

        recent_webhooks.append({"ts": nairobi_now(), "data": data})
        if len(recent_webhooks) > 20:
            recent_webhooks.pop(0)

        phone  = extract_phone(data)
        amount = extract_amount(data)

        if not phone:
            return jsonify({"error": "Could not find M-PESA phone number", "received_keys": list(data.keys())}), 400
        if not amount:
            return jsonify({"error": "Could not find payment amount", "received_keys": list(data.keys())}), 400

        phone_fmt   = format_phone(phone)
        first_name  = extract_first_name(data)
        last_name   = extract_last_name(data)
        name        = extract_name(data)
        meal        = extract_meal(data)
        pickup_time = extract_pickup_time(data)

        logger.info("Initiating STK Push → %s %s | %s | KES %s", first_name, last_name, phone_fmt, amount)

        # Daraja auth + STK Push
        token     = get_access_token()
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

        # Store order details keyed by CheckoutRequestID so the callback can find them
        checkout_id = result.get("CheckoutRequestID")
        if checkout_id:
            pending_payments[checkout_id] = {
                "first_name":  first_name,
                "last_name":   last_name,
                "name":        name,
                "phone":       phone_fmt,
                "meal":        meal,
                "pickup_time": pickup_time,
                "amount":      amount,
                "timestamp":   nairobi_now()
            }
            logger.info("Stored pending payment for CheckoutRequestID: %s", checkout_id)

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
    """
    Daraja calls this after the customer acts on the STK Push.
    On success → writes confirmed order row to Google Sheet.
    On failure → logs it (order is NOT written to Sheet).
    """
    data = request.get_json(force=True) or {}
    logger.info("Payment callback:\n%s", json.dumps(data, indent=2))

    try:
        stk         = data["Body"]["stkCallback"]
        result_code = stk["ResultCode"]
        result_desc = stk["ResultDesc"]
        checkout_id = stk.get("CheckoutRequestID")

        if result_code == 0:
            # ── Payment successful ──────────────────────────────────────────
            items = {i["Name"]: i.get("Value") for i in stk["CallbackMetadata"]["Item"]}
            transaction_id = items.get("MpesaReceiptNumber", "—")
            paid_amount    = items.get("Amount", "—")
            logger.info("✅ Payment SUCCESS — Txn: %s | Amount: KES %s", transaction_id, paid_amount)

            # Retrieve stored order details
            order = pending_payments.pop(checkout_id, {})

            sheet_row = {
                "timestamp":      nairobi_now(),
                "first_name":     order.get("first_name", "—"),
                "last_name":      order.get("last_name", "—"),
                "phone":          order.get("phone", items.get("PhoneNumber", "—")),
                "pickup_time":    order.get("pickup_time", "—"),
                "meal":           order.get("meal", "—"),
                "amount":         paid_amount or order.get("amount", "—"),
                "transaction_id": transaction_id
            }

            logger.info("Writing to Sheet: %s", sheet_row)
            post_to_sheet(sheet_row)

        else:
            # ── Payment failed / cancelled / timed out ──────────────────────
            order = pending_payments.pop(checkout_id, {})
            logger.warning(
                "❌ Payment FAILED (code %s): %s | Order: %s",
                result_code, result_desc, order
            )
            # Order is NOT written to the Sheet — only confirmed payments appear there

    except Exception as exc:
        logger.error("Callback parse error: %s", exc)

    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


@app.route("/debug", methods=["GET"])
def debug():
    return jsonify({
        "pending_payments": pending_payments,
        "recent_webhooks":  recent_webhooks
    })


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "Mtamu M-PESA server is running 🔥", "time": nairobi_now()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
