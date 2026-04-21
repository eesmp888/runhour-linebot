import os
import base64
import httpx
import re
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, ImageMessageContent

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GOOGLE_VISION_API_KEY = os.environ.get("GOOGLE_VISION_API_KEY", "")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


def download_line_image(message_id: str) -> bytes:
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    resp = httpx.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.content


def ocr_image(image_bytes: bytes) -> str:
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    url = f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_API_KEY}"
    payload = {
        "requests": [{
            "image": {"content": image_b64},
            "features": [{"type": "TEXT_DETECTION"}]
        }]
    }
    resp = httpx.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    annotations = data.get("responses", [{}])[0].get("textAnnotations", [])
    if not annotations:
        return ""
    return annotations[0].get("description", "")


def find_runhour(text: str) -> float | None:
    candidates = []
    for line in text.strip().split("\n"):
        for n in re.findall(r'\b\d{4,7}(?:[.,]\d{1,2})?\b', line):
            try:
                candidates.append(float(n.replace(",", ".")))
            except ValueError:
                pass
        compact = re.sub(r'\s+', '', line)
        for n in re.findall(r'\d{5,7}', compact):
            try:
                v = float(n)
                if v not in candidates:
                    candidates.append(v)
            except ValueError:
                pass

    valid = [v for v in candidates if 100 <= v <= 999999]
    return max(valid) if valid else (max(candidates) if candidates else None)


def reply(reply_token: str, text: str):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    try:
        image_bytes = download_line_image(event.message.id)
    except Exception as e:
        reply(event.reply_token, f"❌ ดาวน์โหลดรูปไม่ได้: {e}")
        return

    try:
        raw_text = ocr_image(image_bytes)
    except Exception as e:
        reply(event.reply_token, f"❌ OCR ล้มเหลว: {e}")
        return

    run_hour = find_runhour(raw_text)

    if run_hour is None:
        reply(event.reply_token,
              f"❌ หาตัวเลข Run Hour ไม่ได้\n"
              f"📄 OCR เห็น: {raw_text[:100] or 'ไม่พบข้อความ'}")
    else:
        reply(event.reply_token,
              f"🕐 Run Hour: {run_hour:,.1f} h\n"
              f"📄 OCR เห็น: {raw_text[:80].replace(chr(10), ' ')}")


@app.route("/", methods=["GET"])
def index():
    return "Run Hour Bot is running! 🤖"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
