import os
import json
import re
from datetime import date, datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeSerializer
from dotenv import load_dotenv
from anthropic import Anthropic
from app.database import get_db, init_db

load_dotenv()

SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "changeme")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
signer = URLSafeSerializer(SECRET_KEY)
COOKIE_NAME = "tracker_auth"
USER_COOKIE = "tracker_user"

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def is_authenticated(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        return signer.loads(token) == "authenticated"
    except Exception:
        return False


def current_user_id(request: Request):
    token = request.cookies.get(USER_COOKIE)
    if not token:
        return None
    try:
        return int(signer.loads(token))
    except Exception:
        return None


async def get_user(db, user_id: int):
    cur = await db.execute("SELECT * FROM users WHERE id=?", (user_id,))
    return await cur.fetchone()


async def get_users(db):
    cur = await db.execute("SELECT * FROM users ORDER BY id")
    return await cur.fetchall()


# --- Auth ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    if password == SITE_PASSWORD:
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE_NAME, signer.dumps("authenticated"), max_age=86400 * 30, httponly=True)
        return response
    return templates.TemplateResponse("login.html", {"request": request, "error": "Wrong password"})


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    response.delete_cookie(USER_COOKIE)
    return response


# --- User picker ---

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    if current_user_id(request) is None:
        return RedirectResponse("/pick", status_code=303)
    return RedirectResponse("/today", status_code=303)


@app.get("/pick", response_class=HTMLResponse)
async def pick_user(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    db = await get_db()
    users = await get_users(db)
    await db.close()
    return templates.TemplateResponse("pick_user.html", {"request": request, "users": users})


@app.post("/pick/{user_id}")
async def set_user(user_id: int):
    response = RedirectResponse("/today", status_code=303)
    response.set_cookie(USER_COOKIE, signer.dumps(str(user_id)), max_age=86400 * 365, httponly=True)
    return response


@app.get("/switch")
async def switch_user():
    response = RedirectResponse("/pick", status_code=303)
    response.delete_cookie(USER_COOKIE)
    return response


# --- Daily view ---

@app.get("/today", response_class=HTMLResponse)
async def today(request: Request, d: str = ""):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    user_id = current_user_id(request)
    if user_id is None:
        return RedirectResponse("/pick", status_code=303)

    log_date = d if d else date.today().isoformat()
    db = await get_db()
    user = await get_user(db, user_id)
    if user is None:
        await db.close()
        return RedirectResponse("/pick", status_code=303)

    cur = await db.execute(
        "SELECT * FROM entries WHERE user_id=? AND log_date=? ORDER BY logged_at DESC",
        (user_id, log_date),
    )
    entries = await cur.fetchall()

    cur = await db.execute(
        "SELECT COALESCE(SUM(calories*servings),0) AS cals, COALESCE(SUM(protein_g*servings),0) AS prot FROM entries WHERE user_id=? AND log_date=?",
        (user_id, log_date),
    )
    totals = await cur.fetchone()
    await db.close()

    cals = int(totals["cals"])
    prot = float(totals["prot"])
    cal_pct = min(100, int(cals / user["calorie_target"] * 100)) if user["calorie_target"] else 0
    prot_pct = min(100, int(prot / user["protein_target"] * 100)) if user["protein_target"] else 0

    return templates.TemplateResponse(
        "today.html",
        {
            "request": request,
            "user": user,
            "entries": entries,
            "log_date": log_date,
            "cals": cals,
            "prot": round(prot, 1),
            "cal_pct": cal_pct,
            "prot_pct": prot_pct,
            "cal_remaining": user["calorie_target"] - cals,
            "prot_remaining": round(user["protein_target"] - prot, 1),
        },
    )


# --- Log entry ---

@app.get("/log", response_class=HTMLResponse)
async def log_page(request: Request, q: str = ""):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    user_id = current_user_id(request)
    if user_id is None:
        return RedirectResponse("/pick", status_code=303)

    db = await get_db()
    if q:
        cur = await db.execute(
            "SELECT * FROM foods WHERE name LIKE ? ORDER BY name COLLATE NOCASE LIMIT 50",
            (f"%{q}%",),
        )
    else:
        cur = await db.execute(
            "SELECT * FROM foods ORDER BY created_at DESC LIMIT 50"
        )
    foods = await cur.fetchall()
    await db.close()
    return templates.TemplateResponse(
        "log.html", {"request": request, "foods": foods, "q": q}
    )


@app.post("/log/from-library/{food_id}")
async def log_from_library(request: Request, food_id: int, servings: float = Form(1.0)):
    if not is_authenticated(request):
        raise HTTPException(401)
    user_id = current_user_id(request)
    if user_id is None:
        raise HTTPException(400, "No user selected")
    db = await get_db()
    cur = await db.execute("SELECT * FROM foods WHERE id=?", (food_id,))
    food = await cur.fetchone()
    if food is None:
        await db.close()
        raise HTTPException(404)
    await db.execute(
        "INSERT INTO entries (user_id, food_id, food_name, calories, protein_g, servings) VALUES (?,?,?,?,?,?)",
        (user_id, food["id"], food["name"], food["calories"], food["protein_g"], servings),
    )
    await db.commit()
    await db.close()
    return RedirectResponse("/today", status_code=303)


@app.post("/log/manual")
async def log_manual(
    request: Request,
    name: str = Form(...),
    calories: int = Form(...),
    protein_g: float = Form(...),
    serving_description: str = Form(""),
    save_to_library: str = Form(""),
    servings: float = Form(1.0),
):
    if not is_authenticated(request):
        raise HTTPException(401)
    user_id = current_user_id(request)
    if user_id is None:
        raise HTTPException(400, "No user selected")
    db = await get_db()
    food_id = None
    if save_to_library:
        cur = await db.execute(
            "INSERT INTO foods (name, calories, protein_g, serving_description, source) VALUES (?,?,?,?,'manual')",
            (name, calories, protein_g, serving_description),
        )
        food_id = cur.lastrowid
    await db.execute(
        "INSERT INTO entries (user_id, food_id, food_name, calories, protein_g, servings) VALUES (?,?,?,?,?,?)",
        (user_id, food_id, name, calories, protein_g, servings),
    )
    await db.commit()
    await db.close()
    return RedirectResponse("/today", status_code=303)


@app.post("/entries/{entry_id}/delete")
async def delete_entry(request: Request, entry_id: int):
    if not is_authenticated(request):
        raise HTTPException(401)
    user_id = current_user_id(request)
    db = await get_db()
    await db.execute("DELETE FROM entries WHERE id=? AND user_id=?", (entry_id, user_id))
    await db.commit()
    await db.close()
    return RedirectResponse("/today", status_code=303)


# --- Ask Claude ---

@app.get("/ask", response_class=HTMLResponse)
async def ask_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("ask.html", {"request": request, "result": None, "form": {}})


@app.post("/ask/estimate")
async def ask_estimate(
    request: Request,
    description: str = Form(...),
    calories: str = Form(""),
    protein_g: str = Form(""),
):
    if not is_authenticated(request):
        raise HTTPException(401)
    if anthropic_client is None:
        raise HTTPException(500, "Anthropic API key not configured")

    known_cal = calories.strip()
    known_prot = protein_g.strip()
    needs_cal = known_cal == ""
    needs_prot = known_prot == ""

    if not needs_cal and not needs_prot:
        return templates.TemplateResponse(
            "ask.html",
            {
                "request": request,
                "result": {
                    "name": description,
                    "calories": int(known_cal),
                    "protein_g": float(known_prot),
                    "serving_description": "",
                    "note": "Both values supplied - no estimate needed.",
                },
                "form": {"description": description, "calories": known_cal, "protein_g": known_prot},
            },
        )

    fields_needed = []
    if needs_cal:
        fields_needed.append("calories (whole number)")
        fields_needed.append("calorie_reasoning (one short sentence)")
    if needs_prot:
        fields_needed.append("protein_g (number, grams)")
        fields_needed.append("protein_reasoning (one short sentence)")

    known_block = ""
    if not needs_cal:
        known_block += f"\nKnown calories (do not estimate, treat as truth): {known_cal}"
    if not needs_prot:
        known_block += f"\nKnown protein grams (do not estimate, treat as truth): {known_prot}"

    prompt = f"""You are a nutrition estimator. The user describes a food or meal. Estimate the missing nutrition values for ONE serving as described.

Food description: {description}{known_block}

Return JSON with these keys: {", ".join(fields_needed)}, and serving_description (a short label like "1 sandwich, no bread" or "1 cup").

Return ONLY the JSON object, no prose, no markdown fences."""

    try:
        msg = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        data = json.loads(m.group(0) if m else text)
    except Exception as e:
        return templates.TemplateResponse(
            "ask.html",
            {
                "request": request,
                "result": None,
                "error": f"Claude error: {e}",
                "form": {"description": description, "calories": known_cal, "protein_g": known_prot},
            },
        )

    final_cal = int(known_cal) if not needs_cal else int(data.get("calories", 0))
    final_prot = float(known_prot) if not needs_prot else float(data.get("protein_g", 0))
    serving_desc = data.get("serving_description", "")
    cal_reason = data.get("calorie_reasoning", "") if needs_cal else ""
    prot_reason = data.get("protein_reasoning", "") if needs_prot else ""

    return templates.TemplateResponse(
        "ask.html",
        {
            "request": request,
            "result": {
                "name": description,
                "calories": final_cal,
                "protein_g": final_prot,
                "serving_description": serving_desc,
                "cal_reason": cal_reason,
                "prot_reason": prot_reason,
                "needs_cal": needs_cal,
                "needs_prot": needs_prot,
            },
            "form": {"description": description, "calories": known_cal, "protein_g": known_prot},
        },
    )


@app.post("/ask/log")
async def ask_log(
    request: Request,
    name: str = Form(...),
    calories: int = Form(...),
    protein_g: float = Form(...),
    serving_description: str = Form(""),
    save_to_library: str = Form(""),
    servings: float = Form(1.0),
):
    if not is_authenticated(request):
        raise HTTPException(401)
    user_id = current_user_id(request)
    if user_id is None:
        raise HTTPException(400, "No user selected")
    db = await get_db()
    food_id = None
    if save_to_library:
        cur = await db.execute(
            "INSERT INTO foods (name, calories, protein_g, serving_description, source) VALUES (?,?,?,?,'claude')",
            (name, calories, protein_g, serving_description),
        )
        food_id = cur.lastrowid
    await db.execute(
        "INSERT INTO entries (user_id, food_id, food_name, calories, protein_g, servings) VALUES (?,?,?,?,?,?)",
        (user_id, food_id, name, calories, protein_g, servings),
    )
    await db.commit()
    await db.close()
    return RedirectResponse("/today", status_code=303)


# --- Library management ---

@app.get("/library", response_class=HTMLResponse)
async def library(request: Request, q: str = ""):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    db = await get_db()
    if q:
        cur = await db.execute(
            "SELECT * FROM foods WHERE name LIKE ? ORDER BY name COLLATE NOCASE",
            (f"%{q}%",),
        )
    else:
        cur = await db.execute("SELECT * FROM foods ORDER BY name COLLATE NOCASE")
    foods = await cur.fetchall()
    await db.close()
    return templates.TemplateResponse(
        "library.html", {"request": request, "foods": foods, "q": q}
    )


@app.post("/library/{food_id}/delete")
async def delete_food(request: Request, food_id: int):
    if not is_authenticated(request):
        raise HTTPException(401)
    db = await get_db()
    await db.execute("DELETE FROM foods WHERE id=?", (food_id,))
    await db.commit()
    await db.close()
    return RedirectResponse("/library", status_code=303)


@app.get("/library/{food_id}/edit", response_class=HTMLResponse)
async def edit_food_page(request: Request, food_id: int):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    db = await get_db()
    cur = await db.execute("SELECT * FROM foods WHERE id=?", (food_id,))
    food = await cur.fetchone()
    await db.close()
    if food is None:
        raise HTTPException(404)
    return templates.TemplateResponse("edit_food.html", {"request": request, "food": food})


@app.post("/library/{food_id}/edit")
async def edit_food(
    request: Request,
    food_id: int,
    name: str = Form(...),
    calories: int = Form(...),
    protein_g: float = Form(...),
    serving_description: str = Form(""),
):
    if not is_authenticated(request):
        raise HTTPException(401)
    db = await get_db()
    await db.execute(
        "UPDATE foods SET name=?, calories=?, protein_g=?, serving_description=? WHERE id=?",
        (name, calories, protein_g, serving_description, food_id),
    )
    await db.commit()
    await db.close()
    return RedirectResponse("/library", status_code=303)


# --- Settings ---

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    db = await get_db()
    users = await get_users(db)
    await db.close()
    return templates.TemplateResponse("settings.html", {"request": request, "users": users})


@app.post("/settings/{user_id}")
async def update_targets(
    request: Request,
    user_id: int,
    calorie_target: int = Form(...),
    protein_target: int = Form(...),
):
    if not is_authenticated(request):
        raise HTTPException(401)
    db = await get_db()
    await db.execute(
        "UPDATE users SET calorie_target=?, protein_target=? WHERE id=?",
        (calorie_target, protein_target, user_id),
    )
    await db.commit()
    await db.close()
    return RedirectResponse("/settings", status_code=303)


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    user_id = current_user_id(request)
    if user_id is None:
        return RedirectResponse("/pick", status_code=303)

    db = await get_db()
    user = await get_user(db, user_id)
    if user is None:
        await db.close()
        return RedirectResponse("/pick", status_code=303)

    cur = await db.execute(
        """SELECT log_date,
                  COALESCE(SUM(calories*servings),0) AS cals,
                  COALESCE(SUM(protein_g*servings),0) AS prot
             FROM entries
             WHERE user_id=?
             GROUP BY log_date
             ORDER BY log_date DESC
             LIMIT 60""",
        (user_id,),
    )
    rows = await cur.fetchall()
    await db.close()

    days = []
    for r in rows:
        cals = int(r["cals"])
        prot = float(r["prot"])
        days.append({
            "log_date": r["log_date"],
            "cals": cals,
            "prot": round(prot, 1),
            "cal_pct": int(cals / user["calorie_target"] * 100) if user["calorie_target"] else 0,
            "prot_pct": int(prot / user["protein_target"] * 100) if user["protein_target"] else 0,
        })

    return templates.TemplateResponse(
        "history.html",
        {"request": request, "user": user, "days": days},
    )
