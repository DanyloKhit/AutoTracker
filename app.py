import os
import re
import json
import time
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify
from postgrest.exceptions import APIError
from supabase import create_client, Client

load_dotenv()

app = Flask(__name__)

# sb setup
# This runs on the server only, so use a *secret* key (sb_secret_...), never the
# publishable one. SUPABASE_KEY is accepted as an older name for the same thing.
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError(
        "Missing SUPABASE_URL or SUPABASE_SECRET_KEY. Copy .env.example to .env and fill them in."
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Used if a row is missing from the settings table
DEFAULT_SETTINGS = {
    "gas_price": "1.65",
    "region": "European Region",
    "unit": "L",
    "currency": "$",
}

@app.errorhandler(APIError)
def handle_supabase_error(err):
    return jsonify({"error": f"Database error: {err.message}"}), 500

# helpers

# Wikimedia asks API clients to identify themselves with contact info.
# Put your email or website in .env as WIKIMEDIA_CONTACT.
WIKI_USER_AGENT = "AutoTracker/1.0 ({}) python-urllib".format(
    os.environ.get("WIKIMEDIA_CONTACT", "personal hobby project, set WIKIMEDIA_CONTACT in .env")
)

MAKE_ALIASES = {"vw": "volkswagen", "mercedes": "mercedes-benz", "chevy": "chevrolet"}

LOGO_DOMAINS = {
    'audi': 'audi.com', 'bmw': 'bmw.com', 'mercedes-benz': 'mercedes-benz.com',
    'volkswagen': 'volkswagen.com', 'porsche': 'porsche.com', 'ford': 'ford.com',
    'toyota': 'toyota.com', 'honda': 'honda.com', 'nissan': 'nissan.com',
    'skoda': 'skoda-auto.com', 'seat': 'seat.com', 'cupra': 'cupraofficial.com',
    'hyundai': 'hyundai.com', 'kia': 'kia.com', 'volvo': 'volvocars.com',
    'mazda': 'mazda.com', 'opel': 'opel.com', 'renault': 'renault.com',
    'peugeot': 'peugeot.com', 'citroen': 'citroen.com', 'citroën': 'citroen.com',
    'fiat': 'fiat.com', 'alfa romeo': 'alfaromeo.com', 'lexus': 'lexus.com',
    'subaru': 'subaru.com', 'mitsubishi': 'mitsubishi-motors.com',
    'suzuki': 'suzuki.com', 'chevrolet': 'chevrolet.com', 'jeep': 'jeep.com',
    'dodge': 'dodge.com', 'chrysler': 'chrysler.com', 'tesla': 'tesla.com',
    'land rover': 'landrover.com', 'jaguar': 'jaguar.com', 'mini': 'mini.com',
    'dacia': 'dacia.com', 'lada': 'lada.ru', 'infiniti': 'infiniti.com',
    'acura': 'acura.com', 'cadillac': 'cadillac.com', 'genesis': 'genesis.com',
}

def normalize_make(make):
    make = (make or "").strip()
    return MAKE_ALIASES.get(make.lower(), make)

def logo_for_make(make):
    # Clearbit's logo service was shut down in Dec 2025, so logos now come from
    # Google's favicon service, which needs no key.
    key = normalize_make(make).lower()
    if not key:
        return ""
    domain = LOGO_DOMAINS.get(key) or f"{re.sub(r'[^a-z0-9-]', '', key)}.com"
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=128"

def _clean(value):
    return str(value or "").strip()

def _http_json(url, timeout=5, user_agent=WIKI_USER_AGENT):
    req = urllib.request.Request(url, headers={'User-Agent': user_agent, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))

def _describe_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f"Wikipedia answered with HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"could not reach Wikipedia ({exc.reason})"
    if isinstance(exc, TimeoutError):
        return "Wikipedia took too long to answer"
    return str(exc) or exc.__class__.__name__

# api endpoints

@app.route('/api/nhtsa/vin/<vin>', methods=['GET'])
def decode_vin(vin):
    vin_clean = vin.strip().upper()
    if len(vin_clean) != 17 or any(ch in vin_clean for ch in "IOQ") or not vin_clean.isalnum():
        return jsonify({"error": "VIN must be exactly 17 letters/numbers (I, O and Q are not valid VIN characters)."}), 400

    url = f"https://vpic.nhtsa.dot.gov/api/vehicles/decodevinvalues/{urllib.parse.quote(vin_clean)}?format=json"
    try:
        data = _http_json(url, timeout=8, user_agent='Mozilla/5.0')
        results = (data.get('Results') or [{}])[0]

        make = _clean(results.get("Make")).title()
        model = _clean(results.get("Model"))
        year = _clean(results.get("ModelYear"))
        trim = _clean(results.get("Trim"))
        body_class = _clean(results.get("BodyClass"))
        drive_type = _clean(results.get("DriveType"))
        fuel_primary = _clean(results.get("FuelTypePrimary"))
        cylinders = _clean(results.get("EngineCylinders"))
        displacement = _clean(results.get("DisplacementL"))
        engine_config = _clean(results.get("EngineConfiguration"))
        transmission = _clean(results.get("TransmissionStyle"))
        plant_country = _clean(results.get("PlantCountry"))
        vehicle_type = _clean(results.get("VehicleType"))
        series = _clean(results.get("Series"))

        if not make and not model:
            return jsonify({"error": "NHTSA has no data for this VIN. Its database mostly covers US-market cars, so for many European VINs you'll need to enter the car manually."}), 404

        engine_parts = []
        if displacement:
            try:
                engine_parts.append(f"{round(float(displacement), 1)}L")
            except ValueError:
                engine_parts.append(f"{displacement}L")
        if cylinders: engine_parts.append(f"{cylinders}-Cyl")
        if engine_config: engine_parts.append(f"({engine_config})")
        engine_str = " ".join(engine_parts)

        notes_spec = []
        if engine_str: notes_spec.append(f"Engine: {engine_str}")
        if drive_type: notes_spec.append(f"Drive: {drive_type}")
        if fuel_primary: notes_spec.append(f"Fuel: {fuel_primary}")
        if body_class: notes_spec.append(f"Body: {body_class}")
        if transmission: notes_spec.append(f"Trans: {transmission}")
        if plant_country: notes_spec.append(f"Built in: {plant_country}")

        summary_notes = " | ".join(notes_spec)
        full_trim = " ".join(filter(None, [series, trim, engine_str])).strip()

        return jsonify({
            "vin": vin_clean,
            "make": make,
            "model": model,
            "year": year,
            "trim": full_trim,
            "body_class": body_class,
            "drive_type": drive_type,
            "fuel_type": fuel_primary,
            "engine": engine_str,
            "transmission": transmission,
            "plant_country": plant_country,
            "vehicle_type": vehicle_type,
            "summary_notes": summary_notes
        })
    except Exception as e:
        app.logger.warning("VIN decode failed for %s: %s", vin_clean, e)
        return jsonify({"error": f"Failed to decode VIN: {str(e)}"}), 500

@app.route('/api/nhtsa/models', methods=['GET'])
def get_nhtsa_models():
    make = normalize_make(request.args.get('make', ''))
    year = request.args.get('year', '').strip()
    if not make or not year:
        return jsonify([])
    
    url = f"https://vpic.nhtsa.dot.gov/api/vehicles/GetModelsForMakeYear/make/{urllib.parse.quote(make)}/modelyear/{year}?format=json"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=4) as response:
            data = json.loads(response.read().decode())
            models = [item['Model_Name'] for item in data.get('Results', [])]
            return jsonify(sorted(list(set(models))))
    except Exception:
        return jsonify([])

@app.route('/api/trims', methods=['GET'])
def get_trims():
    make = normalize_make(request.args.get('make', ''))
    model = request.args.get('model', '').strip()
    year = request.args.get('year', '').strip()
    
    trims = []
    if make and model:
        try:
            url = f"https://www.carqueryapi.com/api/0.3/?cmd=getTrims&make={urllib.parse.quote(make)}&model={urllib.parse.quote(model)}"
            if year:
                url += f"&year={year}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=3) as response:
                raw = response.read().decode('utf-8')
                if raw.startswith("?(") and raw.endswith(");"):
                    raw = raw[2:-2]
                data = json.loads(raw)
                for item in data.get('Trims', []):
                    t_name = item.get('model_trim', '')
                    t_engine = item.get('model_engine_cc', '')
                    if t_engine:
                        t_engine = f"{round(float(t_engine)/1000, 1)}L"
                    t_series = item.get('model_series', '')
                    full_t = " ".join(filter(None, [t_series, t_name, t_engine])).strip()
                    if full_t and full_t not in trims:
                        trims.append(full_t)
        except Exception:
            pass

    if not trims:
        make_lower = make.lower()
        model_lower = model.lower()
        
        model_trims_db = {
            ('audi', 'a3'): ['1.4 TFSI', '1.5 TFSI', '2.0 TDI', '30 TFSI', '35 TFSI', '35 TDI', '40 TFSI', '40 TDI', 'S-Line', 'Black Edition', 'Quattro', 'Advanced', 'Proline'],
            ('audi', 'a4'): ['1.8 TFSI', '2.0 TFSI', '2.0 TDI', '3.0 TDI', '35 TFSI', '40 TFSI', '45 TFSI', '50 TDI', 'S-Line', 'Quattro', 'Black Edition', 'Vorsprung', 'Avant'],
            ('audi', 'a6'): ['2.0 TDI', '3.0 TDI', '40 TDI', '45 TFSI', '50 TDI', '55 TFSI e', 'S-Line', 'Quattro', 'Black Edition', 'Avant'],
            ('audi', 'tt'): ['1.8 TFSI', '2.0 TFSI', '2.0 TDI', '45 TFSI', 'Quattro', 'S-Line', 'TTS', 'Black Edition'],
            ('audi', 'q5'): ['2.0 TDI', '2.0 TFSI', '40 TDI', '45 TFSI', '50 TDI', '55 TFSI e', 'S-Line', 'Quattro', 'Vorsprung'],
            ('bmw', '3 series'): ['318i', '320i', '320d', '330i', '330e', 'M340i', 'M Sport', 'xDrive', 'sDrive', 'Shadowline', 'Touring'],
            ('bmw', '3'): ['318i', '320i', '320d', '330i', '330e', 'M340i', 'M Sport', 'xDrive', 'sDrive', 'Shadowline'],
            ('bmw', '5 series'): ['520i', '520d', '530i', '530e', '540i', '550i', 'M Sport', 'xDrive', 'Luxury Line'],
            ('bmw', '5'): ['520i', '520d', '530i', '530e', '540i', '550i', 'M Sport', 'xDrive'],
            ('volkswagen', 'golf'): ['1.0 TSI', '1.4 TSI', '1.5 TSI', '2.0 TDI', 'GTI', 'GTD', 'GTE', 'R-Line', 'Life', 'Style', 'Match'],
            ('volkswagen', 'passat'): ['1.5 TSI', '2.0 TDI', '2.0 TSI', 'GTE', 'R-Line', 'SE Business', 'Elegance', '4MOTION', 'Variant'],
            ('mercedes', 'c-class'): ['C 180', 'C 200', 'C 220 d', 'C 300', 'C 300 e', 'AMG Line', '4MATIC', 'Night Package', 'Estate'],
            ('mercedes-benz', 'c-class'): ['C 180', 'C 200', 'C 220 d', 'C 300', 'C 300 e', 'AMG Line', '4MATIC', 'Night Package', 'Estate'],
            ('skoda', 'octavia'): ['1.0 TSI', '1.5 TSI', '2.0 TDI', 'vRS', 'iV Plug-in', 'SE L', 'Laurin & Klement', 'Sportline'],
            ('seat', 'leon'): ['1.0 TSI', '1.5 TSI', '2.0 TDI', 'FR', 'FR Sport', 'SE Technology', 'XCELLENCE']
        }

        matched_model_trims = None
        for (m_make, m_model), t_list in model_trims_db.items():
            if m_make == make_lower and m_model in model_lower:
                matched_model_trims = t_list
                break

        if matched_model_trims:
            trims = matched_model_trims
        else:
            european_brand_trims = {
                'audi': ['30 TFSI', '35 TFSI', '35 TDI', '40 TFSI', '40 TDI', 'S-Line', 'Quattro', 'Black Edition', 'Advanced'],
                'bmw': ['118i', '320i', '320d', '330i', '520d', 'M Sport', 'xDrive', 'sDrive', 'Shadowline'],
                'mercedes': ['A 180', 'C 220 d', 'C 300 e', 'E 220 d', 'AMG Line', '4MATIC', 'Night Package'],
                'mercedes-benz': ['A 180', 'C 220 d', 'C 300 e', 'E 220 d', 'AMG Line', '4MATIC', 'Night Package'],
                'volkswagen': ['1.0 TSI', '1.5 TSI', '2.0 TDI', 'GTE', 'GTI', 'GTD', 'R-Line', '4MOTION'],
                'skoda': ['1.0 TSI', '1.5 TSI', '2.0 TDI', 'Monte Carlo', 'Laurin & Klement', 'Sportline'],
                'seat': ['1.0 TSI', '1.5 TSI', '2.0 TDI', 'FR', 'FR Sport', 'SE Technology'],
                'cupra': ['VZ', 'e-HYBRID', 'VZ2', 'VZ3', 'Design Edition']
            }
            generic_trims = [
                '30 TFSI', '35 TFSI', '35 TDI', '40 TFSI', '2.0 TDI', '1.5 TSI', 
                'S-Line', 'AWD / Quattro', 'Plug-in Hybrid', 'DSG / Automatic'
            ]
            trims = european_brand_trims.get(make_lower, generic_trims)

    return jsonify(trims[:15])

def find_wikipedia_image(make, model, year):
    """Search Wikipedia for a photo of this car. Returns (thumbnail_url, score).

    Prefers an article about the right year/generation. Raises only if every
    request failed, so "nothing found" and "Wikipedia unreachable" stay distinct.
    """
    queries = []
    if year:
        queries.extend([
            f'"{year}" "{make}" "{model}" car generation',
            f'{make} {model} {year} model',
        ])
    queries.append(f'{make} {model} automobile generation')

    make_words = [w for w in re.split(r'[^a-z0-9]+', make.lower()) if w]
    model_words = [w for w in re.split(r'[^a-z0-9]+', model.lower()) if w]

    best = None
    best_score = -1
    seen = set()
    errors = []
    deadline = time.monotonic() + 12

    for search_query in queries:
        if time.monotonic() > deadline:
            break
        wiki_url = (
            "https://en.wikipedia.org/w/api.php?action=query&generator=search"
            f"&gsrsearch={urllib.parse.quote(search_query)}&gsrlimit=15"
            "&prop=pageimages|extracts&exintro=1&explaintext=1&exchars=900"
            "&pithumbsize=1200&format=json"
        )
        try:
            payload = _http_json(wiki_url, timeout=5)
        except Exception as exc:
            errors.append(exc)
            app.logger.warning("Wikipedia search failed (%s): %s", search_query, exc)
            continue

        for page in payload.get('query', {}).get('pages', {}).values():
            title = page.get('title', '')
            title_l = title.lower()
            extract_l = page.get('extract', '').lower()
            thumb = page.get('thumbnail', {}).get('source', '')
            if not thumb or title in seen:
                continue
            seen.add(title)
            if any(term in title_l for term in ('disambiguation', 'list of', 'automotive industry')):
                continue
            # Require make/model evidence in the article itself.
            if not all(w in (title_l + ' ' + extract_l) for w in make_words):
                continue
            if not all(w in (title_l + ' ' + extract_l) for w in model_words):
                continue
            score = 0
            if all(w in title_l for w in make_words): score += 12
            if all(w in title_l for w in model_words): score += 18
            generation_specific = any(term in title_l for term in ('generation', 'b5', 'b6', 'b7', 'b8', 'b9', 'e46', 'e90', 'mk'))
            if year:
                if year in title_l:
                    score += 80
                elif year in extract_l:
                    score += 22
                    # An article that merely mentions the year may illustrate a newer model.
                    if not generation_specific:
                        score -= 35
                else:
                    score -= 8
            # Prefer model/generation articles over broad brand pages.
            if generation_specific:
                score += 12
            if title_l.strip() in (f'{make.lower()} {model.lower()}', f'{make.lower()} {model.lower()} (automobile)'):
                score -= 20
            if score > best_score:
                best_score = score
                best = thumb
        # Stop early only when a strong, year-specific article was found.
        if year and best and best_score >= 90:
            break

    if best is None and errors and len(errors) == len(queries):
        raise errors[-1]
    return best, best_score

def find_wikipedia_summary_image(make, model):
    """Fallback: the lead photo of the plain "Make Model" article."""
    titles = []
    for t in (f"{make} {model}", f"{make.title()} {model}", f"{make.title()} {model} (automobile)"):
        if t not in titles:
            titles.append(t)
    for title in titles:
        url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(' ', '_'), safe='')
        try:
            data = _http_json(url, timeout=5)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        if data.get('type') == 'disambiguation':
            continue
        thumb = (data.get('thumbnail') or {}).get('source', '')
        if thumb:
            return thumb
    return ''

@app.route('/api/car-media', methods=['GET'])
def get_car_media():
    make = normalize_make(request.args.get('make', ''))
    model = request.args.get('model', '').strip()
    year = request.args.get('year', '').strip()

    result = {"logo": logo_for_make(make), "image": "", "status": "not_found", "approximate": False, "message": ""}
    if not (make and model):
        return jsonify(result)

    try:
        best, best_score = find_wikipedia_image(make, model, year)
        # With a year, a low-scoring article may show a different generation, so skip it.
        if best and (not year or best_score >= 10):
            result.update(image=best, status="ok")
        else:
            fallback = find_wikipedia_summary_image(make, model)
            if fallback:
                # The main article usually shows the newest generation, so say so.
                result.update(image=fallback, status="ok", approximate=bool(year))
    except Exception as exc:
        app.logger.warning("Car photo lookup failed for %s %s %s: %s", make, model, year, exc)
        result.update(status="error", message=_describe_error(exc))

    return jsonify(result)

@app.route('/api/cars', methods=['GET', 'POST'])
def manage_cars():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        make = str(data.get('make', '')).strip()
        model = str(data.get('model', '')).strip()
        if not make or not model:
            return jsonify({"error": "Make and model are required."}), 400
        try:
            year = int(data.get('year'))
        except (TypeError, ValueError):
            return jsonify({"error": "Year must be a number."}), 400
        if not 1885 <= year <= datetime.now().year + 2:
            return jsonify({"error": "Year is out of range."}), 400
        image_url = str(data.get('image_url', '')).strip()
        if not image_url.startswith(('http://', 'https://')):
            image_url = ''
        res = supabase.table('cars').insert({
            "make": make,
            "model": model,
            "year": year,
            "trim": data.get('trim', ''),
            "notes": data.get('notes', ''),
            "image_url": image_url,
            "logo_url": logo_for_make(make),
        }).execute()
        return jsonify({"status": "success", "id": res.data[0]['id']})
    else:
        res = supabase.table('cars').select('*').order('id', desc=True).execute()
        cars = res.data
        for car in cars:
            # Logos are derived from the make, so this also repairs older rows
            # that still point at the retired Clearbit service.
            car['logo_url'] = logo_for_make(car.get('make', ''))
        return jsonify(cars)

@app.route('/api/cars/<int:car_id>', methods=['DELETE', 'PUT'])
def update_delete_car(car_id):
    if request.method == 'DELETE':
        # crashes, gas_logs and maintenance rows go with it (ON DELETE CASCADE)
        supabase.table('cars').delete().eq('id', car_id).execute()
        return jsonify({"status": "deleted"})
    elif request.method == 'PUT':
        data = request.get_json(silent=True) or {}
        supabase.table('cars').update({"notes": data.get('notes', '')}).eq('id', car_id).execute()
        return jsonify({"status": "updated"})

@app.route('/api/cars/<int:car_id>/crashes', methods=['GET', 'POST'])
def car_crashes(car_id):
    if request.method == 'POST':
        data = request.json
        data = data or {}
        description = str(data.get('description', '')).strip()
        if not description:
            return jsonify({"error": "A crash description is required."}), 400
        date_str = str(data.get('date', '')).strip()
        if date_str:
            try:
                parsed = datetime.fromisoformat(date_str)
                date_str = parsed.strftime("%Y-%m-%d %H:%M") if 'T' in date_str else parsed.strftime("%Y-%m-%d")
            except ValueError:
                return jsonify({"error": "Date must use YYYY-MM-DD format."}), 400
        else:
            date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            cost = float(data.get('cost', 0))
            if cost < 0:
                raise ValueError()
        except (TypeError, ValueError):
            return jsonify({"error": "Cost must be a non-negative number."}), 400
        supabase.table('crashes').insert({
            "car_id": car_id,
            "date": date_str,
            "description": description,
            "cost": cost,
        }).execute()
        return jsonify({"status": "success"})
    else:
        res = supabase.table('crashes').select('*').eq('car_id', car_id).order('id', desc=True).execute()
        return jsonify(res.data)

@app.route('/api/cars/<int:car_id>/gas', methods=['GET', 'POST'])
def car_gas(car_id):
    if request.method == 'POST':
        data = request.json
        price_res = supabase.table('settings').select('value').eq('key', 'gas_price').execute()
        current_price = float(price_res.data[0]['value']) if price_res.data else float(DEFAULT_SETTINGS['gas_price'])
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        supabase.table('gas_logs').insert({
            "car_id": car_id,
            "date": date_str,
            "distance": float(data['distance']),
            "fuel_amount": float(data['fuel_amount']),
            "price_per_unit": current_price,
        }).execute()
        return jsonify({"status": "success"})
    else:
        res = supabase.table('gas_logs').select('*').eq('car_id', car_id).order('id', desc=True).execute()
        return jsonify(res.data)

@app.route('/api/cars/<int:car_id>/maintenance', methods=['GET', 'POST'])
def car_maintenance(car_id):
    if request.method == 'POST':
        data = request.json
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        supabase.table('maintenance').insert({
            "car_id": car_id,
            "date": date_str,
            "service_type": data['service_type'],
            "odometer": float(data.get('odometer', 0)),
            "cost": float(data.get('cost', 0)),
            "notes": data.get('notes', ''),
        }).execute()
        return jsonify({"status": "success"})
    else:
        res = supabase.table('maintenance').select('*').eq('car_id', car_id).order('id', desc=True).execute()
        return jsonify(res.data)

@app.route('/api/settings', methods=['GET', 'POST'])
def regional_settings():
    if request.method == 'POST':
        data = request.json
        rows = [
            {"key": "gas_price", "value": str(data['gas_price'])},
            {"key": "region", "value": str(data['region'])},
            {"key": "unit", "value": str(data['unit'])},
        ]
        if 'currency' in data:
            rows.append({"key": "currency", "value": str(data['currency'])})
        supabase.table('settings').upsert(rows, on_conflict='key').execute()
        return jsonify({"status": "updated"})
    else:
        res = supabase.table('settings').select('key,value').execute()
        settings = dict(DEFAULT_SETTINGS)
        settings.update({row['key']: row['value'] for row in res.data})
        return jsonify(settings)

@app.route('/')
def home():
    return render_template('index.html')

if __name__ == '__main__':
    app.run(debug=True, port=5000)
