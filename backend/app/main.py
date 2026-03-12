from typing import List

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session, joinedload

from app.database import Base, engine, get_db
from app.models import MenuSource, Recipe, RecipeIngredient
from app.schemas import MenuSourceOut, ParseMenuRequest
from services.menu_parser import (
    fetch_menu_text_from_url,
    parse_menu_with_gemini,
    save_parsed_menu,
)
from services.nutrition_service import get_ingredient_nutrition

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Pathway RFP Pipeline")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "message": "Pathway RFP backend is running",
        "step": "1 and 2",
        "health": "/health",
        "docs": "/docs",
    }


@app.get("/health")
def health_check():
    return {"status": "ok", "step": "1 and 2"}


@app.post("/step1/parse-menu", response_model=MenuSourceOut)
def step1_parse_menu(payload: ParseMenuRequest, db: Session = Depends(get_db)):
    try:
        payload.validate_input()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        if payload.menu_url:
            raw_menu_text = fetch_menu_text_from_url(str(payload.menu_url))
            source_type = "url"
            source_value = str(payload.menu_url)
        else:
            raw_menu_text = payload.menu_text.strip()
            source_type = "text"
            source_value = raw_menu_text

        parsed_menu = parse_menu_with_gemini(
            menu_text=raw_menu_text,
            restaurant_name=payload.restaurant_name,
        )

        menu_source = save_parsed_menu(
            db=db,
            parsed_menu=parsed_menu,
            source_type=source_type,
            source_value=source_value,
            raw_menu_text=raw_menu_text,
            restaurant_name=payload.restaurant_name,
        )

        saved = (
            db.query(MenuSource)
            .options(
                joinedload(MenuSource.recipes)
                .joinedload(Recipe.ingredients)
                .joinedload(RecipeIngredient.ingredient)
            )
            .filter(MenuSource.id == menu_source.id)
            .first()
        )

        return transform_menu_source(saved)

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/step1/menus", response_model=List[MenuSourceOut])
def list_parsed_menus(db: Session = Depends(get_db)):
    menu_sources = (
        db.query(MenuSource)
        .options(
            joinedload(MenuSource.recipes)
            .joinedload(Recipe.ingredients)
            .joinedload(RecipeIngredient.ingredient)
        )
        .order_by(MenuSource.id.desc())
        .all()
    )

    return [transform_menu_source(item) for item in menu_sources]


@app.get("/step2/recipe-nutrition/{recipe_id}")
def recipe_nutrition(recipe_id: int, db: Session = Depends(get_db)):
    recipe = (
        db.query(Recipe)
        .options(
            joinedload(Recipe.ingredients).joinedload(RecipeIngredient.ingredient)
        )
        .filter(Recipe.id == recipe_id)
        .first()
    )

    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")

    nutrition_totals = {
        "calories": 0.0,
        "protein": 0.0,
        "fat": 0.0,
        "carbs": 0.0,
    }

    ingredient_results = []

    for item in recipe.ingredients:
        ingredient_name = item.ingredient.name

        nutrition = get_ingredient_nutrition(
            ingredient_name=ingredient_name,
            quantity=item.quantity,
            unit=item.unit,
        )

        if nutrition and nutrition.get("scaled"):
            nutrition_totals["calories"] += nutrition.get("calories", 0.0)
            nutrition_totals["protein"] += nutrition.get("protein", 0.0)
            nutrition_totals["fat"] += nutrition.get("fat", 0.0)
            nutrition_totals["carbs"] += nutrition.get("carbs", 0.0)

        ingredient_results.append(
            {
                "ingredient": ingredient_name,
                "quantity": item.quantity,
                "unit": item.unit,
                "nutrition": nutrition,
            }
        )

    nutrition_totals = {
        "calories": round(nutrition_totals["calories"], 2),
        "protein": round(nutrition_totals["protein"], 2),
        "fat": round(nutrition_totals["fat"], 2),
        "carbs": round(nutrition_totals["carbs"], 2),
    }

    return {
        "recipe_id": recipe.id,
        "recipe": recipe.dish_name,
        "ingredients": ingredient_results,
        "total_nutrition": nutrition_totals,
    }