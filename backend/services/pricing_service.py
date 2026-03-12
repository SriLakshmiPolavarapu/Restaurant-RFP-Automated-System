import json
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import requests
from requests.auth import HTTPBasicAuth
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Ingredient, IngredientPriceSnapshot

PRICE_KEYWORDS = {
    "price",
    "weighted_average",
    "weighted_avg",
    "weighted_average_price",
    "weighted_avg_price",
    "average_price",
    "avg_price",
    "price_avg",
    "low_price",
    "high_price",
    "price_low",
    "price_high",
}

UNIT_CANDIDATES = ["unit", "uom", "package", "packaging", "unit_of_sale"]
DATE_CANDIDATES = ["report_date", "reported_date", "date", "publication_date"]
MARKET_CANDIDATES = ["market", "market_name", "location", "city"]
OFFICE_CANDIDATES = ["office", "office_name"]
TITLE_CANDIDATES = ["report_title", "title"]

INGREDIENT_TO_COMMODITY = {
    "romaine lettuce": "Lettuce",
    "lettuce": "Lettuce",
    "ahi tuna": "Tuna",
    "yellow tail tuna": "Tuna",
    "tuna": "Tuna",
    "salmon fillet": "Salmon",
    "salmon": "Salmon",
    "chicken": "Chicken",
    "chicken breast": "Chicken",
    "oysters": "Oysters",
    "eggplant": "Eggplant",
    "apple": "Apples",
    "fuji apple": "Apples",
    "sweet potato": "Sweet Potatoes",
    "fig": "Figs",
    "tomato": "Tomatoes",
    "tomatoes": "Tomatoes",
    "cucumber": "Cucumbers",
    "carrots": "Carrots",
    "avocado": "Avocados",
    "bacon": "Pork",
    "parmesan cheese": "Cheese",
    "bleu cheese": "Cheese",
    "cheese": "Cheese",
    "bread": "Bread",
}


def get_report_ids() -> List[str]:
    return [item.strip() for item in settings.USDA_MARS_REPORT_IDS.split(",") if item.strip()]


def normalize_ingredient_to_commodity(ingredient_name: str) -> str:
    name = ingredient_name.strip().lower()

    if name in INGREDIENT_TO_COMMODITY:
        return INGREDIENT_TO_COMMODITY[name]

    for key, value in INGREDIENT_TO_COMMODITY.items():
        if key in name:
            return value

    return ingredient_name.title()


def parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    match = re.search(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not match:
        return None

    try:
        return float(match.group())
    except ValueError:
        return None


def get_first_present(data: Dict[str, Any], keys: List[str]) -> Optional[str]:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return str(data[key])
    return None


def request_report(report_id: str, commodity_name: str, start_date: str, end_date: str) -> Any:
    url = f"{settings.USDA_MARS_BASE_URL}/reports/{report_id}"
    params = {
        "q": f"commodity={commodity_name};report_begin_date={start_date}:{end_date}",
        "allSections": "true",
    }

    response = requests.get(
        url,
        params=params,
        auth=HTTPBasicAuth(settings.USDA_MMN_API_KEY, ""),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def flatten_dict_nodes(payload: Any) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []

    def walk(obj: Any):
        if isinstance(obj, dict):
            nodes.append(obj)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(payload)
    return nodes


def extract_price_snapshots_from_payload(
    payload: Any,
    ingredient_id: int,
    commodity_name: str,
    report_id: str,
) -> List[Dict[str, Any]]:
    snapshots: List[Dict[str, Any]] = []
    nodes = flatten_dict_nodes(payload)

    for node in nodes:
        lower_keys = {k.lower(): k for k in node.keys()}

        values = {}
        for candidate in ["price_low", "low_price", "low"]:
            if candidate in lower_keys:
                values["price_low"] = parse_float(node[lower_keys[candidate]])
                break

        for candidate in ["price_high", "high_price", "high"]:
            if candidate in lower_keys:
                values["price_high"] = parse_float(node[lower_keys[candidate]])
                break

        for candidate in ["price_avg", "avg_price", "average_price", "weighted_average", "weighted_avg", "price"]:
            if candidate in lower_keys:
                values["price_avg"] = parse_float(node[lower_keys[candidate]])
                break

        if not any(values.get(k) is not None for k in ["price_low", "price_high", "price_avg"]):
            continue

        report_title = get_first_present(node, TITLE_CANDIDATES)
        market_name = get_first_present(node, MARKET_CANDIDATES)
        office_name = get_first_present(node, OFFICE_CANDIDATES)
        unit = get_first_present(node, UNIT_CANDIDATES)
        report_date = get_first_present(node, DATE_CANDIDATES)

        snapshots.append(
            {
                "ingredient_id": ingredient_id,
                "commodity_name": commodity_name,
                "report_id": str(report_id),
                "report_title": report_title,
                "market_name": market_name,
                "office_name": office_name,
                "price_low": values.get("price_low"),
                "price_high": values.get("price_high"),
                "price_avg": values.get("price_avg"),
                "unit": unit,
                "report_date": report_date,
                "source": "USDA_MMN",
                "raw_payload": json.dumps(node),
            }
        )

    return snapshots


def store_price_snapshots(db: Session, snapshots: List[Dict[str, Any]]) -> int:
    stored = 0

    for snapshot in snapshots:
        exists = (
            db.query(IngredientPriceSnapshot)
            .filter(
                IngredientPriceSnapshot.ingredient_id == snapshot["ingredient_id"],
                IngredientPriceSnapshot.report_id == snapshot["report_id"],
                IngredientPriceSnapshot.report_date == snapshot["report_date"],
                IngredientPriceSnapshot.price_avg == snapshot["price_avg"],
                IngredientPriceSnapshot.commodity_name == snapshot["commodity_name"],
            )
            .first()
        )

        if exists:
            continue

        db.add(IngredientPriceSnapshot(**snapshot))
        stored += 1

    db.commit()
    return stored


def fetch_and_store_pricing_for_ingredient(
    db: Session,
    ingredient: Ingredient,
    lookback_days: int = 30,
) -> Dict[str, Any]:
    commodity_name = normalize_ingredient_to_commodity(ingredient.name)
    end_date = date.today()
    start_date = end_date - timedelta(days=lookback_days)

    start_str = start_date.strftime("%m/%d/%Y")
    end_str = end_date.strftime("%m/%d/%Y")

    total_found = 0
    total_stored = 0
    errors: List[str] = []

    for report_id in get_report_ids():
        try:
            payload = request_report(
                report_id=report_id,
                commodity_name=commodity_name,
                start_date=start_str,
                end_date=end_str,
            )
            snapshots = extract_price_snapshots_from_payload(
                payload=payload,
                ingredient_id=ingredient.id,
                commodity_name=commodity_name,
                report_id=report_id,
            )
            total_found += len(snapshots)
            total_stored += store_price_snapshots(db, snapshots)
        except Exception as exc:
            errors.append(f"report {report_id}: {exc}")

    return {
        "ingredient_id": ingredient.id,
        "ingredient_name": ingredient.name,
        "commodity_name": commodity_name,
        "snapshots_found": total_found,
        "snapshots_stored": total_stored,
        "errors": errors,
    }


def build_trend_summary(db: Session, ingredient_id: int) -> Dict[str, Any]:
    ingredient = db.query(Ingredient).filter(Ingredient.id == ingredient_id).first()
    if not ingredient:
        raise ValueError("Ingredient not found")

    snapshots = (
        db.query(IngredientPriceSnapshot)
        .filter(IngredientPriceSnapshot.ingredient_id == ingredient_id)
        .order_by(IngredientPriceSnapshot.created_at.asc(), IngredientPriceSnapshot.id.asc())
        .all()
    )

    valid = [s for s in snapshots if s.price_avg is not None]

    if not snapshots:
        return {
            "ingredient_id": ingredient.id,
            "ingredient_name": ingredient.name,
            "commodity_name": None,
            "snapshot_count": 0,
            "latest_price_avg": None,
            "min_price_avg": None,
            "max_price_avg": None,
            "avg_price_avg": None,
            "trend": "no data",
            "latest_unit": None,
            "snapshots": [],
        }

    if not valid:
        trend = "insufficient price data"
        latest_price_avg = None
        min_price_avg = None
        max_price_avg = None
        avg_price_avg = None
        latest_unit = snapshots[-1].unit
    else:
        latest = valid[-1]
        first = valid[0]

        latest_price_avg = latest.price_avg
        min_price_avg = min(s.price_avg for s in valid)
        max_price_avg = max(s.price_avg for s in valid)
        avg_price_avg = round(sum(s.price_avg for s in valid) / len(valid), 2)
        latest_unit = latest.unit

        if len(valid) == 1:
            trend = "single snapshot"
        else:
            change = latest.price_avg - first.price_avg
            if abs(change) < 0.01:
                trend = "stable"
            elif change > 0:
                trend = "up"
            else:
                trend = "down"

    return {
        "ingredient_id": ingredient.id,
        "ingredient_name": ingredient.name,
        "commodity_name": snapshots[-1].commodity_name if snapshots else None,
        "snapshot_count": len(snapshots),
        "latest_price_avg": latest_price_avg,
        "min_price_avg": min_price_avg,
        "max_price_avg": max_price_avg,
        "avg_price_avg": avg_price_avg,
        "trend": trend,
        "latest_unit": latest_unit,
        "snapshots": snapshots,
    }