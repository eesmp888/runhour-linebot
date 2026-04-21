import os
import json
import base64
import httpx
from datetime import datetime
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, ImageMessageContent
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GOOGLE_VISION_API_KEY = os.environ.get("GOOGLE_VISION_API_KEY", "")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


def get_google_sheet():
    """Connect to Google Sheets"""
    if not GOOGLE_CREDENTIALS_JSON or not GOOGLE_SHEET_ID:
        return None
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        worksheet = sh.sheet1
        # Add header if sheet is empty
        if worksheet.row_count == 0 or worksheet.cell(1, 1).value != "วันที่":
            worksheet.insert_row(
                ["วันที่", "เวลา", "Machine ID", "Run Hour (h)", "ความมั่นใจ AI", "ส่งโดย", "หมายเหตุ"],
                index=1
            )
        return worksheet
    except Exception as e:
        print(f"Google Sheets error: {e}")
        return None


def read_runhour_from_image(image_bytes: bytes) -> dict:
    """Use Google Cloud Vision OCR to read run hour value from meter image"""
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    url = f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_API_KEY}"
    payload = {
        "requests": [{
            "image": {"content": image_b64},
            "features": [{"type": "TEXT_DETECTION"}]
        }]
    }

    try:
        resp = httpx.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        # Extract all text from OCR result
        annotations = data.get("responses", [{}])[0].get("textAnnotations", [])
        if not annotations:
            return None

        full_text = annotations[0].get("description", "")
        print(f"OCR raw text: {full_text}")

        # Parse numbers from OCR text — find the run hour value
        # Look for large number sequences (odometer style: 5-7 digits, may have decimal)
        import re

        # Clean text — remove spaces between digits (OCR sometimes splits digits)
        lines = full_text.strip().split("\n")

        # Strategy: find longest numeric sequence that looks like a meter reading
        candidates = []
        for line in lines:
            # Match patterns like: 93125, 93125.7, 9 3 1 2 5 (with spaces)
            # First try: normal number with optional decimal
            nums = re.findall(r'\b\d{4,7}(?:[.,]\d{1,2})?\b', line)
            for n in nums:
                try:
                    val = float(n.replace(",", "."))
                    candidates.append(val)
                except ValueError:
                    pass

            # Second try: digits separated by spaces on same line (e.g. "9 3 1 2 5")
            compact = re.sub(r'\s+', '', line)
            nums2 = re.findall(r'\d{5,7}', compact)
            for n in nums2:
                try:
                    val = float(n)
                    if val not in candidates:
                        candidates.append(val)
                except ValueError:
                    pass

        if not candidates:
            return None

        # Pick the most likely run hour value:
        # - Prefer values between 100 and 999999 (reasonable run hours)
        # - Prefer larger numbers (main display, not partial)
        valid = [v for v in candidates if 100 <= v <= 999999]
        if not valid:
            valid = candidates

        value = max(valid)  # largest plausible value = main meter reading

        # Confidence based on how clean the reading is
        confidence = "high" if len(valid) == 1 else "medium"

        # Try to find machine label in text (letters+numbers near top)
        machine_hint = ""
        for line in lines[:5]:
            label_match = re.search(r'\b[A-Z]\d{1,3}\b', line.upper())
            if label_match:
                machine_hint = label_match.group()
                break

        return {
            "value": value,
            "confidence": confidence,
            "machine_hint": machine_hint,
            "note_th": f"OCR อ่านได้: {full_text[:80].replace(chr(10), ' ')}"
        }

    except Exception as e:
        print(f"Google Vision API error: {e}")
        return None


def download_line_image(message_id: str) -> bytes:
    """Download image from LINE Content API"""
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    resp = httpx.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.content


def reply_message(reply_token: str, text: str):
    """Send reply back to LINE"""
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
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


def parse_caption(caption: str) -> tuple[str, str]:
    """
    Parse image caption from technician.
    Returns (machine_id, extra_note)

    Supported formats:
      "A16"                → machine_id="A16", note=""
      "A16 ตรวจรายเดือน"  → machine_id="A16", note="ตรวจรายเดือน"
      "GEN-01"             → machine_id="GEN-01", note=""
      ""                   → machine_id="", note=""
    """
    if not caption:
        return "", ""
    parts = caption.strip().split(None, 1)  # split on first whitespace only
    machine_id = parts[0].upper() if parts else ""
    extra_note = parts[1].strip() if len(parts) > 1 else ""
    return machine_id, extra_note


@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image(event):
    reply_token = event.reply_token
    sender_name = ""
    try:
        sender_name = event.source.user_id or ""
    except Exception:
        pass

    # --- Read caption (Machine ID) sent with image ---
    caption = ""
    try:
        caption = (event.message.image_set or {}).get("id", "") or ""
    except Exception:
        pass
    # LINE SDK v3: caption is in event.message.file_name or via raw body
    # Most reliable: check message object attributes
    try:
        if hasattr(event.message, "caption") and event.message.caption:
            caption = event.message.caption
    except Exception:
        pass

    caption_machine_id, caption_note = parse_caption(caption)

    # Step 1: Notify processing — tell user if Machine ID was detected
    if caption_machine_id:
        ack = f"📷 รับรูปแล้ว (Machine: {caption_machine_id})\nกำลังอ่านค่า Run Hour... ⏳"
    else:
        ack = "📷 รับรูปแล้ว กำลังอ่านค่า Run Hour... ⏳\n💡 ส่งรูปพร้อมแคปชั่นเพื่อระบุ Machine เช่น: A16"
    reply_message(reply_token, ack)

    # Step 2: Download image
    try:
        image_bytes = download_line_image(event.message.id)
    except Exception as e:
        print(f"Image download error: {e}")
        return

    # Step 3: AI read value
    result = read_runhour_from_image(image_bytes)

    now = datetime.now()
    date_str = now.strftime("%d/%m/%Y")
    time_str = now.strftime("%H:%M:%S")

    if not result:
        msg = (
            "❌ ไม่สามารถอ่านค่าได้อัตโนมัติ\n"
            "กรุณาแก้ไขด้วยตนเอง:\n"
            f"แก้ไข {caption_machine_id or '<Machine ID>'} <ค่า Run Hour>\n"
            f"ตัวอย่าง: แก้ไข {caption_machine_id or 'A16'} 93125.7"
        )
        push_to_group(event, msg)
        return

    value = result.get("value", 0)
    confidence = result.get("confidence", "low")
    ai_machine_hint = result.get("machine_hint", "")
    note_th = result.get("note_th", "")

    # Caption Machine ID takes priority over AI hint
    final_machine_id = caption_machine_id or ai_machine_hint or "ไม่ระบุ"
    # Combine notes
    combined_note = " | ".join(filter(None, [note_th, caption_note]))

    machine_source = "จากแคปชั่น ✍️" if caption_machine_id else "AI อ่านจากรูป 🤖"
    confidence_th = {"high": "สูง ✅", "medium": "ปานกลาง ⚠️", "low": "ต่ำ ❌"}.get(confidence, "ไม่ทราบ")

    # Step 4: Save to Google Sheets
    sheet_saved = False
    try:
        ws = get_google_sheet()
        if ws:
            ws.append_row([
                date_str,
                time_str,
                final_machine_id,
                value,
                confidence_th,
                sender_name,
                combined_note,
            ])
            sheet_saved = True
    except Exception as e:
        print(f"Sheet save error: {e}")

    sheet_status = "✅ บันทึก Google Sheets แล้ว" if sheet_saved else "⚠️ ยังไม่ได้เชื่อม Google Sheets"

    msg = (
        f"✅ อ่านค่า Run Hour สำเร็จ!\n"
        f"{'─'*25}\n"
        f"🕐 Run Hour  : {value:,.1f} h\n"
        f"📅 วันที่    : {date_str} {time_str}\n"
        f"🏷️  Machine  : {final_machine_id}  ({machine_source})\n"
        f"🤖 ความมั่นใจ: {confidence_th}\n"
        f"📝 หมายเหตุ  : {combined_note or '-'}\n"
        f"{'─'*25}\n"
        f"{sheet_status}\n\n"
        f"💡 แก้ไขค่า? พิมพ์:\n"
        f"แก้ไข {final_machine_id} <ค่าที่ถูก>\n"
        f"เช่น: แก้ไข {final_machine_id} 93125.7"
    )
    push_to_group(event, msg)


def push_to_group(event, text: str):
    """Push message to group/user"""
    try:
        source = event.source
        if hasattr(source, "group_id") and source.group_id:
            target_id = source.group_id
        elif hasattr(source, "room_id") and source.room_id:
            target_id = source.room_id
        else:
            target_id = source.user_id

        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(
                {"to": target_id, "messages": [{"type": "text", "text": text}]}
            )
    except Exception as e:
        print(f"Push message error: {e}")


@handler.add(MessageEvent)
def handle_text(event):
    """Handle text messages for manual entry and correction"""
    try:
        text = event.message.text.strip()
    except AttributeError:
        return

    reply_token = event.reply_token

    # Manual correction: "แก้ไข A16 93125.7"
    if text.startswith("แก้ไข "):
        parts = text.split()
        if len(parts) >= 3:
            machine_id = parts[1]
            try:
                value = float(parts[2])
                now = datetime.now()

                ws = get_google_sheet()
                if ws:
                    ws.append_row([
                        now.strftime("%d/%m/%Y"),
                        now.strftime("%H:%M:%S"),
                        machine_id,
                        value,
                        "Manual ✏️",
                        "",
                        "แก้ไขด้วยตนเอง",
                    ])
                    reply_message(reply_token, f"✏️ บันทึกค่าแก้ไขแล้ว!\nMachine: {machine_id}\nRun Hour: {value:,.1f} h")
                else:
                    reply_message(reply_token, f"✏️ รับค่าแล้ว: {machine_id} = {value:,.1f} h\n⚠️ ยังไม่ได้เชื่อม Google Sheets")
            except ValueError:
                reply_message(reply_token, "❌ รูปแบบไม่ถูกต้อง\nใช้: แก้ไข <Machine ID> <ค่า>\nเช่น: แก้ไข A16 93125.7")
        return

    # Help command
    if text in ["ช่วยเหลือ", "help", "วิธีใช้"]:
        reply_message(
            reply_token,
            "🤖 Run Hour Bot วิธีใช้\n"
            "─────────────────────\n"
            "📷 ส่งรูปมิเตอร์ → AI อ่านค่าอัตโนมัติ\n\n"
            "✏️ แก้ไขค่า:\nพิมพ์: แก้ไข <Machine> <ค่า>\nเช่น: แก้ไข A16 93125.7\n\n"
            "📊 ข้อมูลบันทึกลง Google Sheets อัตโนมัติ"
        )


@app.route("/", methods=["GET"])
def index():
    return "Run Hour LINE Bot is running! 🤖"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
