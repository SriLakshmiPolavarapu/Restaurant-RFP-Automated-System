import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from app.models import (
    Distributor,
    Ingredient,
    IngredientDistributorMatch,
    MenuSource,
    Recipe,
    RecipeIngredient,
    RFPEmail,
)

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "") 
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "") or SMTP_USER
SENDER_NAME = os.getenv("SENDER_NAME", "Pathway RFP System")

MOCK_MODE = os.getenv("EMAIL_MOCK_MODE", "true").lower() in ("true", "1", "yes")

QUOTE_DEADLINE_DAYS = int(os.getenv("QUOTE_DEADLINE_DAYS", "7"))



def compose_rfp_email(
    restaurant_name: str,
    distributor_name: str,
    ingredients: list[dict],
    deadline: str,
    sender_name: str = SENDER_NAME,
) -> dict:
   
    subject = f"Request for Quote — {restaurant_name} Ingredient Sourcing"

    ing_rows_text = ""
    ing_rows_html = ""
    for i, ing in enumerate(ingredients, 1):
        qty_str = f"{ing['quantity']} {ing['unit']}" if ing.get('quantity') else "TBD"
        dishes_str = ", ".join(ing.get("dishes", []))
        ing_rows_text += f"  {i}. {ing['name'].title()} — Qty: {qty_str} (used in: {dishes_str})\n"
        ing_rows_html += f"""
        <tr>
            <td style="padding:8px 12px; border-bottom:1px solid #eee;">{ing['name'].title()}</td>
            <td style="padding:8px 12px; border-bottom:1px solid #eee;">{qty_str}</td>
            <td style="padding:8px 12px; border-bottom:1px solid #eee; color:#666;">{dishes_str}</td>
        </tr>"""

    body_text = f"""Dear {distributor_name},

We are reaching out on behalf of {restaurant_name} to request a price quote for the following ingredients. We are currently evaluating suppliers and would appreciate your best pricing.

INGREDIENTS NEEDED:
{ing_rows_text}
DEADLINE FOR QUOTE: {deadline}

Please reply to this email with:
  1. Unit price for each ingredient listed
  2. Minimum order quantities (if any)
  3. Delivery schedule and lead time
  4. Any bulk discount tiers available

We look forward to your response.

Best regards,
{sender_name}
{restaurant_name}
"""

    body_html = f"""
<div style="font-family: 'Helvetica Neue', Arial, sans-serif; max-width: 640px; margin: 0 auto; color: #333;">
    <div style="background: #e85d26; padding: 24px 32px; border-radius: 8px 8px 0 0;">
        <h1 style="color: #fff; margin: 0; font-size: 20px;">Request for Quote</h1>
        <p style="color: rgba(255,255,255,0.85); margin: 4px 0 0; font-size: 14px;">{restaurant_name} — Ingredient Sourcing</p>
    </div>

    <div style="background: #fff; padding: 32px; border: 1px solid #e0e0e0; border-top: none; border-radius: 0 0 8px 8px;">
        <p>Dear <strong>{distributor_name}</strong>,</p>

        <p>We are reaching out on behalf of <strong>{restaurant_name}</strong> to request a price quote for the following ingredients. We are currently evaluating suppliers and would appreciate your best pricing.</p>

        <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
            <thead>
                <tr style="background: #f8f8f8;">
                    <th style="padding: 10px 12px; text-align: left; font-size: 12px; text-transform: uppercase; color: #888; border-bottom: 2px solid #e0e0e0;">Ingredient</th>
                    <th style="padding: 10px 12px; text-align: left; font-size: 12px; text-transform: uppercase; color: #888; border-bottom: 2px solid #e0e0e0;">Est. Quantity</th>
                    <th style="padding: 10px 12px; text-align: left; font-size: 12px; text-transform: uppercase; color: #888; border-bottom: 2px solid #e0e0e0;">Used In</th>
                </tr>
            </thead>
            <tbody>{ing_rows_html}
            </tbody>
        </table>

        <div style="background: #fff4e6; border-left: 4px solid #e85d26; padding: 12px 16px; margin: 20px 0; border-radius: 0 4px 4px 0;">
            <strong style="color: #e85d26;">Quote Deadline:</strong> {deadline}
        </div>

        <p>Please reply with:</p>
        <ol style="color: #555; line-height: 1.8;">
            <li>Unit price for each ingredient listed</li>
            <li>Minimum order quantities (if any)</li>
            <li>Delivery schedule and lead time</li>
            <li>Any bulk discount tiers available</li>
        </ol>

        <p>We look forward to your response.</p>

        <p style="margin-top: 32px;">
            Best regards,<br>
            <strong>{sender_name}</strong><br>
            <span style="color: #888;">{restaurant_name}</span>
        </p>
    </div>
</div>
"""

    return {
        "subject": subject,
        "body_text": body_text.strip(),
        "body_html": body_html.strip(),
    }

def send_email_smtp(
    to_email: str,
    subject: str,
    body_text: str,
    body_html: str,
) -> dict:
    if MOCK_MODE or not SMTP_USER or not SMTP_PASSWORD:
        return {
            "sent": True,
            "mock": True,
            "message": f"[MOCK] Email to {to_email} logged (SMTP not configured or mock mode enabled)",
        }

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
        msg["To"] = to_email
        msg["Subject"] = subject

        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)

        return {"sent": True, "mock": False, "message": f"Email sent to {to_email}"}

    except Exception as e:
        return {"sent": False, "mock": False, "message": f"SMTP error: {str(e)}"}

def send_rfp_emails_for_menu(db: Session, menu_source_id: int) -> dict:
    
    menu_source = db.query(MenuSource).filter(MenuSource.id == menu_source_id).first()
    if not menu_source:
        raise ValueError(f"Menu source {menu_source_id} not found")

    restaurant_name = menu_source.restaurant_name or "Our Restaurant"

    matches = (
        db.query(IngredientDistributorMatch)
        .options(
            joinedload(IngredientDistributorMatch.ingredient),
            joinedload(IngredientDistributorMatch.distributor),
        )
        .filter(IngredientDistributorMatch.menu_source_id == menu_source_id)
        .all()
    )

    if not matches:
        raise ValueError(f"No distributor matches found for menu source {menu_source_id}. Run Step 3 first.")

    recipes = (
        db.query(Recipe)
        .options(
            joinedload(Recipe.ingredients).joinedload(RecipeIngredient.ingredient)
        )
        .filter(Recipe.menu_source_id == menu_source_id)
        .all()
    )

    ingredient_details = {}
    for recipe in recipes:
        for ri in recipe.ingredients:
            ing_id = ri.ingredient.id
            if ing_id not in ingredient_details:
                ingredient_details[ing_id] = {
                    "name": ri.ingredient.name,
                    "quantity": ri.quantity,
                    "unit": ri.unit,
                    "dishes": [],
                }
            ingredient_details[ing_id]["dishes"].append(recipe.dish_name)

    distributor_ingredients = {}
    for match in matches:
        d_id = match.distributor_id
        if d_id not in distributor_ingredients:
            distributor_ingredients[d_id] = {
                "distributor": match.distributor,
                "ingredients": [],
            }

        ing_info = ingredient_details.get(match.ingredient_id, {
            "name": match.ingredient.name,
            "quantity": None,
            "unit": None,
            "dishes": [],
        })

        distributor_ingredients[d_id]["ingredients"].append({
            "name": ing_info["name"],
            "quantity": ing_info.get("quantity"),
            "unit": ing_info.get("unit"),
            "dishes": ing_info.get("dishes", []),
        })

    deadline = (datetime.now(timezone.utc) + timedelta(days=QUOTE_DEADLINE_DAYS)).strftime("%B %d, %Y")

    email_results = []

    for d_id, data in distributor_ingredients.items():
        distributor = data["distributor"]
        ingredients = data["ingredients"]

        to_email = distributor.email or f"{distributor.name.lower().replace(' ', '.')}@mock-distributor.example.com"

        email_content = compose_rfp_email(
            restaurant_name=restaurant_name,
            distributor_name=distributor.name,
            ingredients=ingredients,
            deadline=deadline,
        )

        send_result = send_email_smtp(
            to_email=to_email,
            subject=email_content["subject"],
            body_text=email_content["body_text"],
            body_html=email_content["body_html"],
        )

        rfp_email = RFPEmail(
            menu_source_id=menu_source_id,
            distributor_id=d_id,
            to_email=to_email,
            subject=email_content["subject"],
            body_text=email_content["body_text"],
            body_html=email_content["body_html"],
            ingredient_count=len(ingredients),
            quote_deadline=deadline,
            status="sent_mock" if send_result.get("mock") else ("sent" if send_result["sent"] else "failed"),
            error_message=None if send_result["sent"] else send_result.get("message"),
        )
        db.add(rfp_email)

        email_results.append({
            "distributor_id": d_id,
            "distributor_name": distributor.name,
            "to_email": to_email,
            "subject": email_content["subject"],
            "ingredient_count": len(ingredients),
            "ingredients": [ing["name"] for ing in ingredients],
            "deadline": deadline,
            "status": rfp_email.status,
            "message": send_result["message"],
        })

    db.commit()

    return {
        "menu_source_id": menu_source_id,
        "restaurant_name": restaurant_name,
        "emails_sent": len(email_results),
        "mock_mode": MOCK_MODE,
        "quote_deadline": deadline,
        "results": email_results,
    }


def list_rfp_emails_for_menu(db: Session, menu_source_id: int) -> list:
    return (
        db.query(RFPEmail)
        .options(joinedload(RFPEmail.distributor))
        .filter(RFPEmail.menu_source_id == menu_source_id)
        .order_by(RFPEmail.created_at.desc())
        .all()
    )


def get_rfp_email_detail(db: Session, email_id: int) -> Optional[RFPEmail]:
    return (
        db.query(RFPEmail)
        .options(joinedload(RFPEmail.distributor))
        .filter(RFPEmail.id == email_id)
        .first()
    )