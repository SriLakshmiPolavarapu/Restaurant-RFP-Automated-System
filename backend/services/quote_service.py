import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import google.generativeai as genai
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func

from app.models import (
    Distributor,
    DistributorQuote,
    DistributorQuoteItem,
    Ingredient,
    IngredientDistributorMatch,
    MenuSource,
    Recipe,
    RecipeIngredient,
    RFPEmail,
)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")


PARSE_QUOTE_PROMPT = """You are a procurement analyst. Parse the following email reply from a food distributor into structured price quotes.

Return ONLY valid JSON in this format:
{
  "items": [
    {
      "ingredient_name": "tomatoes",
      "unit_price": 2.50,
      "unit": "lb",
      "minimum_order_quantity": 10,
      "minimum_order_unit": "lb",
      "notes": "Roma variety, delivered within 24hrs"
    }
  ],
  "delivery_lead_days": 2,
  "delivery_notes": "Free delivery on orders over $200",
  "payment_terms": "Net 30",
  "valid_until": "2026-04-01",
  "general_notes": "Any general notes from the distributor"
}

Rules:
- Extract EVERY ingredient mentioned with its price.
- If a price is given as a range, use the midpoint.
- unit_price should be a single number (float).
- If minimum order isn't mentioned, set to null.
- If delivery terms aren't mentioned, set to null.
- Normalize ingredient names to lowercase.

DISTRIBUTOR EMAIL REPLY:
{email_text}
"""


def parse_quote_with_gemini(email_text: str) -> dict:
  
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")

    prompt = PARSE_QUOTE_PROMPT.format(email_text=email_text)

    try:
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.1,
                max_output_tokens=4096,
                response_mime_type="application/json",
            ),
        )

        raw = response.text.strip()
        first_brace = raw.find('{')
        last_brace = raw.rfind('}')
        if first_brace != -1 and last_brace != -1:
            raw = raw[first_brace:last_brace + 1]

        return json.loads(raw)

    except Exception as e:
        print(f"[Quote Parse] Error: {e}", flush=True)
        return {"items": [], "error": str(e)}

def save_parsed_quote(
    db: Session,
    menu_source_id: int,
    distributor_id: int,
    raw_email_text: str,
    parsed: dict,
) -> DistributorQuote:

    quote = DistributorQuote(
        menu_source_id=menu_source_id,
        distributor_id=distributor_id,
        raw_email_text=raw_email_text,
        delivery_lead_days=parsed.get("delivery_lead_days"),
        delivery_notes=parsed.get("delivery_notes"),
        payment_terms=parsed.get("payment_terms"),
        valid_until=parsed.get("valid_until"),
        general_notes=parsed.get("general_notes"),
        status="received",
    )
    db.add(quote)
    db.flush()

    for item_data in parsed.get("items", []):
        ing_name = item_data.get("ingredient_name", "").strip().lower()

        ingredient = db.query(Ingredient).filter(
            func.lower(Ingredient.name) == ing_name
        ).first()

        quote_item = DistributorQuoteItem(
            quote_id=quote.id,
            ingredient_id=ingredient.id if ingredient else None,
            ingredient_name=ing_name,
            unit_price=item_data.get("unit_price"),
            unit=item_data.get("unit"),
            minimum_order_quantity=item_data.get("minimum_order_quantity"),
            minimum_order_unit=item_data.get("minimum_order_unit"),
            notes=item_data.get("notes"),
        )
        db.add(quote_item)

    db.commit()
    db.refresh(quote)
    return quote

def receive_and_process_quote(
    db: Session,
    menu_source_id: int,
    distributor_id: int,
    email_text: str,
) -> dict:
    
    distributor = db.query(Distributor).filter(Distributor.id == distributor_id).first()
    if not distributor:
        raise ValueError(f"Distributor {distributor_id} not found")

    parsed = parse_quote_with_gemini(email_text)
    print(f"[Step 5] Parsed {len(parsed.get('items', []))} items from {distributor.name}", flush=True)

    quote = save_parsed_quote(db, menu_source_id, distributor_id, email_text, parsed)

    expected_ingredients = _get_expected_ingredients(db, menu_source_id, distributor_id)
    quoted_names = {item.get("ingredient_name", "").lower() for item in parsed.get("items", [])}
    missing = [ing for ing in expected_ingredients if ing.lower() not in quoted_names]

    if missing:
        quote.status = "incomplete"
    else:
        quote.status = "complete"
    db.commit()

    return {
        "quote_id": quote.id,
        "distributor_id": distributor_id,
        "distributor_name": distributor.name,
        "items_received": len(parsed.get("items", [])),
        "items_expected": len(expected_ingredients),
        "missing_ingredients": missing,
        "status": quote.status,
        "delivery_lead_days": parsed.get("delivery_lead_days"),
        "payment_terms": parsed.get("payment_terms"),
    }


def _get_expected_ingredients(db: Session, menu_source_id: int, distributor_id: int) -> list[str]:
    matches = (
        db.query(IngredientDistributorMatch)
        .options(joinedload(IngredientDistributorMatch.ingredient))
        .filter(
            IngredientDistributorMatch.menu_source_id == menu_source_id,
            IngredientDistributorMatch.distributor_id == distributor_id,
        )
        .all()
    )
    return [m.ingredient.name for m in matches]



def generate_followup_email(
    db: Session,
    quote_id: int,
) -> dict:
    quote = (
        db.query(DistributorQuote)
        .options(
            joinedload(DistributorQuote.distributor),
            joinedload(DistributorQuote.items),
        )
        .filter(DistributorQuote.id == quote_id)
        .first()
    )

    if not quote:
        raise ValueError(f"Quote {quote_id} not found")

    if quote.status == "complete":
        return {"message": "Quote is already complete, no follow-up needed."}

    expected = _get_expected_ingredients(db, quote.menu_source_id, quote.distributor_id)
    quoted_names = {item.ingredient_name.lower() for item in quote.items}
    missing = [ing for ing in expected if ing.lower() not in quoted_names]

    if not missing:
        quote.status = "complete"
        db.commit()
        return {"message": "All ingredients are quoted, no follow-up needed."}

    missing_list = "\n".join(f"  - {ing.title()}" for ing in missing)

    subject = f"Follow-Up: Missing Quotes for {len(missing)} Ingredients"
    body_text = f"""Dear {quote.distributor.name},

Thank you for your recent price quote. We noticed that quotes for the following ingredients were not included:

{missing_list}

Could you please provide pricing for these items as well? We'd like to finalize our supplier selection soon.

Thank you,
Pathway RFP System"""

    body_html = f"""
<div style="font-family: 'Helvetica Neue', Arial, sans-serif; max-width: 640px; margin: 0 auto; color: #333;">
    <div style="background: #e85d26; padding: 20px 28px; border-radius: 8px 8px 0 0;">
        <h2 style="color: #fff; margin: 0; font-size: 18px;">Follow-Up: Missing Quotes</h2>
    </div>
    <div style="background: #fff; padding: 28px; border: 1px solid #e0e0e0; border-top: none; border-radius: 0 0 8px 8px;">
        <p>Dear <strong>{quote.distributor.name}</strong>,</p>
        <p>Thank you for your recent price quote. We noticed that quotes for the following ingredients were not included:</p>
        <ul style="color: #e85d26; line-height: 2;">
            {"".join(f'<li style="color: #333;">{ing.title()}</li>' for ing in missing)}
        </ul>
        <p>Could you please provide pricing for these items as well? We'd like to finalize our supplier selection soon.</p>
        <p>Thank you,<br><strong>Pathway RFP System</strong></p>
    </div>
</div>"""

    return {
        "quote_id": quote_id,
        "distributor_id": quote.distributor_id,
        "distributor_name": quote.distributor.name,
        "missing_count": len(missing),
        "missing_ingredients": missing,
        "followup_email": {
            "subject": subject,
            "body_text": body_text.strip(),
            "body_html": body_html.strip(),
        },
    }


def compare_quotes_for_menu(db: Session, menu_source_id: int) -> dict:
    
    quotes = (
        db.query(DistributorQuote)
        .options(
            joinedload(DistributorQuote.distributor),
            joinedload(DistributorQuote.items),
        )
        .filter(DistributorQuote.menu_source_id == menu_source_id)
        .all()
    )

    if not quotes:
        raise ValueError(f"No quotes found for menu source {menu_source_id}")

    comparison = {}
    distributor_summaries = {}

    for quote in quotes:
        d_name = quote.distributor.name
        total_cost = 0.0
        item_count = 0

        for item in quote.items:
            ing_name = item.ingredient_name.lower()

            if ing_name not in comparison:
                comparison[ing_name] = {}

            comparison[ing_name][d_name] = {
                "unit_price": item.unit_price,
                "unit": item.unit,
                "min_order": item.minimum_order_quantity,
                "notes": item.notes,
            }

            if item.unit_price:
                total_cost += item.unit_price
                item_count += 1

        avg_price = total_cost / item_count if item_count > 0 else 0

        distributor_summaries[d_name] = {
            "distributor_id": quote.distributor_id,
            "quote_id": quote.id,
            "items_quoted": item_count,
            "total_estimated_cost": round(total_cost, 2),
            "average_item_price": round(avg_price, 2),
            "delivery_lead_days": quote.delivery_lead_days,
            "payment_terms": quote.payment_terms,
            "delivery_notes": quote.delivery_notes,
            "status": quote.status,
        }

    comparison_table = []
    for ing_name, dist_prices in sorted(comparison.items()):
        row = {"ingredient": ing_name, "quotes": {}}
        best_price = None
        best_distributor = None

        for d_name, price_info in dist_prices.items():
            row["quotes"][d_name] = price_info
            if price_info["unit_price"] is not None:
                if best_price is None or price_info["unit_price"] < best_price:
                    best_price = price_info["unit_price"]
                    best_distributor = d_name

        row["best_price"] = best_price
        row["best_distributor"] = best_distributor
        comparison_table.append(row)

    recommendation = _generate_recommendation(distributor_summaries, comparison_table)

    return {
        "menu_source_id": menu_source_id,
        "quotes_received": len(quotes),
        "ingredients_compared": len(comparison_table),
        "distributor_summaries": distributor_summaries,
        "comparison_table": comparison_table,
        "recommendation": recommendation,
    }


RECOMMENDATION_PROMPT = """You are a restaurant procurement advisor. Based on the distributor quotes below, provide a clear recommendation on which distributor(s) to use.

Consider: total cost, price per item, delivery lead times, payment terms, and coverage (how many ingredients each quotes).

Return ONLY valid JSON:
{{
  "recommended_distributor": "Name",
  "reasoning": "2-3 sentence explanation",
  "strategy": "single_supplier|split_suppliers",
  "cost_savings_pct": 15.0,
  "detailed_notes": "Any additional procurement advice"
}}

DISTRIBUTOR SUMMARIES:
{summaries_json}

COMPARISON TABLE (sample of first 10):
{comparison_json}
"""


def _generate_recommendation(distributor_summaries: dict, comparison_table: list) -> dict:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.0-flash")

        prompt = RECOMMENDATION_PROMPT.format(
            summaries_json=json.dumps(distributor_summaries, indent=2),
            comparison_json=json.dumps(comparison_table[:10], indent=2),
        )

        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.2,
                max_output_tokens=2048,
                response_mime_type="application/json",
            ),
        )

        raw = response.text.strip()
        first_brace = raw.find('{')
        last_brace = raw.rfind('}')
        if first_brace != -1 and last_brace != -1:
            raw = raw[first_brace:last_brace + 1]

        return json.loads(raw)

    except Exception as e:
        print(f"[Recommendation] Error: {e}", flush=True)
        if distributor_summaries:
            best = min(
                distributor_summaries.items(),
                key=lambda x: x[1]["total_estimated_cost"] if x[1]["total_estimated_cost"] > 0 else float('inf')
            )
            return {
                "recommended_distributor": best[0],
                "reasoning": f"Lowest total estimated cost at ${best[1]['total_estimated_cost']}",
                "strategy": "single_supplier",
                "cost_savings_pct": None,
                "detailed_notes": "Recommendation based on cost only (AI recommendation unavailable)",
            }
        return {"recommended_distributor": None, "reasoning": "No quotes to compare"}



def simulate_distributor_replies(db: Session, menu_source_id: int) -> list[dict]:
   
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")

    rfp_emails = (
        db.query(RFPEmail)
        .options(joinedload(RFPEmail.distributor))
        .filter(RFPEmail.menu_source_id == menu_source_id)
        .all()
    )

    if not rfp_emails:
        raise ValueError(f"No RFP emails found for menu source {menu_source_id}. Run Step 4 first.")

    results = []

    for rfp_email in rfp_emails:
        distributor = rfp_email.distributor

        sim_prompt = f"""You are a food distributor named "{distributor.name}" (category: {distributor.category}).
Write a realistic email reply to the RFP below. Include specific prices for as many ingredients as you can reasonably supply given your category. Use realistic wholesale prices.

For a {distributor.category} distributor, only quote ingredients you'd realistically carry.
Skip ingredients outside your specialty.
Include delivery terms and minimum orders.

RFP EMAIL:
{rfp_email.body_text}

Write the reply as plain text (not JSON). Write it as an actual email reply would look."""

        try:
            response = model.generate_content(
                sim_prompt,
                generation_config=genai.types.GenerationConfig(
                    temperature=0.7,
                    max_output_tokens=2048,
                ),
            )

            simulated_reply = response.text.strip()
            print(f"[Step 5] Simulated reply from {distributor.name} ({len(simulated_reply)} chars)", flush=True)

            result = receive_and_process_quote(
                db=db,
                menu_source_id=menu_source_id,
                distributor_id=distributor.id,
                email_text=simulated_reply,
            )
            results.append(result)

        except Exception as e:
            print(f"[Step 5] Error simulating reply from {distributor.name}: {e}", flush=True)
            results.append({
                "distributor_name": distributor.name,
                "error": str(e),
            })

    return results

def list_quotes_for_menu(db: Session, menu_source_id: int) -> list:
    return (
        db.query(DistributorQuote)
        .options(
            joinedload(DistributorQuote.distributor),
            joinedload(DistributorQuote.items),
        )
        .filter(DistributorQuote.menu_source_id == menu_source_id)
        .order_by(DistributorQuote.created_at.desc())
        .all()
    )