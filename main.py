from flask import Flask, render_template, jsonify, request, send_from_directory
import requests
import networkx as nx
from geopy.distance import geodesic
import random
import os
from dotenv import load_dotenv
from cachetools import TTLCache
import logging
from flask_cors import CORS
import time
from functools import wraps

# Configuración inicial
load_dotenv()
app = Flask(__name__)
CORS(app)

# Configuración de logging
logging.basicConfig(level=logging.INFO,
                   format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                   handlers=[logging.FileHandler('app.log'), logging.StreamHandler()])
logger = logging.getLogger(__name__)

# Configuración de caché
CACHE = TTLCache(maxsize=100, ttl=3600)  # Caché de 1 hora
POI_CACHE = TTLCache(maxsize=100, ttl=86400)  # Caché de POIs de 24 horas

# Configuración de API keys y constantes
OPENWEATHERMAP_API_KEY = os.getenv('OPENWEATHERMAP_API_KEY')
if not OPENWEATHERMAP_API_KEY:
    logger.error("API key de OpenWeatherMap no encontrada")

# Centro predeterminado de Puno
DEFAULT_LAT = -15.8403
DEFAULT_LON = -70.0217

def timed_lru_cache(timeout: int = 300):
    def decorator(f):
        cache = TTLCache(maxsize=100, ttl=timeout)
        
        @wraps(f)
        def wrapper(*args, **kwargs):
            key = str(args) + str(kwargs)
            try:
                return cache[key]
            except KeyError:
                result = f(*args, **kwargs)
                cache[key] = result
                return result
        return wrapper
    return decorator

def error_handler(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except requests.RequestException as e:
            logger.error(f"Error en solicitud HTTP: {str(e)}")
            return jsonify({"error": "Error de conexión"}), 503
        except Exception as e:
            logger.error(f"Error inesperado: {str(e)}")
            return jsonify({"error": "Error interno del servidor"}), 500
    return wrapper

@timed_lru_cache(timeout=3600)
def fetch_pois(category):
    """
    Obtiene puntos de interés de OpenStreetMap usando Overpass API.
    """
    queries = {
        'hotel': """
        [out:json][timeout:25];
        area["name"="Puno"]->.puno;
        (
            node["tourism"="hotel"](area.puno);
            way["tourism"="hotel"](area.puno);
            node["amenity"="hotel"](area.puno);
            way["amenity"="hotel"](area.puno);
        );
        out body;
        >;
        out skel qt;
        """,
        'restaurant': """
        [out:json][timeout:25];
        area["name"="Puno"]->.puno;
        (
            node["amenity"="restaurant"](area.puno);
            way["amenity"="restaurant"](area.puno);
        );
        out body;
        >;
        out skel qt;
        """,
        'attraction': """
        [out:json][timeout:25];
        area["name"="Puno"]->.puno;
        (
            node["tourism"="attraction"](area.puno);
            way["tourism"="attraction"](area.puno);
            node["tourism"="museum"](area.puno);
            way["tourism"="museum"](area.puno);
            node["tourism"="viewpoint"](area.puno);
            way["tourism"="viewpoint"](area.puno);
        );
        out body;
        >;
        out skel qt;
        """
    }
    
    if category not in queries:
        logger.warning(f"Categoría no válida solicitada: {category}")
        return []
    
    cache_key = f'pois_{category}'
    if cache_key in POI_CACHE:
        return POI_CACHE[cache_key]
    
    query = queries[category]
    overpass_url = "http://overpass-api.de/api/interpreter"
    
    try:
        response = requests.get(overpass_url, params={'data': query}, timeout=30)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        logger.error(f"Error al obtener POIs de Overpass API: {e}")
        return []

    pois = []
    for element in data.get('elements', []):
        try:
            if element.get('type') == 'node':
                lat, lon = element['lat'], element['lon']
            elif element.get('type') == 'way':
                # Para ways, usamos el centro aproximado
                lats = [node['lat'] for node in data['elements'] if node['type'] == 'node' and node['id'] in element['nodes']]
                lons = [node['lon'] for node in data['elements'] if node['type'] == 'node' and node['id'] in element['nodes']]
                if lats and lons:
                    lat, lon = sum(lats)/len(lats), sum(lons)/len(lons)
                else:
                    continue
            
            tags = element.get('tags', {})
            name = tags.get('name', 'Sin nombre')
            if name == 'Sin nombre':
                continue  # Saltamos lugares sin nombre
            
            poi = {
                "name": name,
                "location": [lat, lon],
                "address": tags.get('addr:street', 'Sin dirección'),
                "type": category,
                "rating": round(random.uniform(3.5, 5.0), 1),  # Calificación simulada más realista
                "description": tags.get('description', ''),
                "phone": tags.get('phone', ''),
                "website": tags.get('website', ''),
                "opening_hours": tags.get('opening_hours', '')
            }
            pois.append(poi)
        except Exception as e:
            logger.error(f"Error al procesar POI: {e}")
            continue

    POI_CACHE[cache_key] = pois
    return pois

@timed_lru_cache(timeout=300)
def fetch_weather(lat, lon):
    """
    Obtiene datos del clima de OpenWeatherMap API.
    """
    if not OPENWEATHERMAP_API_KEY:
        return None

    base_url = "http://api.openweathermap.org/data/2.5/weather"
    params = {
        "lat": lat,
        "lon": lon,
        "appid": OPENWEATHERMAP_API_KEY,
        "units": "metric",
        "lang": "es"
    }
    
    try:
        response = requests.get(base_url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        return {
            "description": data['weather'][0]['description'].capitalize(),
            "temperature": round(data['main']['temp']),
            "feels_like": round(data['main']['feels_like']),
            "humidity": data['main']['humidity'],
            "wind_speed": round(data['wind']['speed'] * 3.6, 1),  # Convertir a km/h
            "icon": data['weather'][0]['icon']
        }
    except requests.RequestException as e:
        logger.error(f"Error al obtener datos del clima: {e}")
        return None

def calculate_routes(user_location, pois):
    """
    Calcula rutas desde la ubicación del usuario a los POIs.
    """
    routes = []
    for poi in pois:
        try:
            poi_location = tuple(poi['location'])
            distance = geodesic(user_location, poi_location).kilometers
            
            routes.append({
                'poi': poi,
                'distance': round(distance, 2)
            })
        except Exception as e:
            logger.error(f"Error al calcular ruta para {poi['name']}: {e}")
    
    routes.sort(key=lambda x: x['distance'])
    return routes

@app.route('/')
def index():
    try:
        return render_template('index.html')
    except Exception as e:
        logger.error(f"Error al renderizar index.html: {e}")
        return "Error al cargar la página", 500

@app.route('/pois/<category>')
@error_handler
def get_pois(category):
    """
    Endpoint para obtener POIs filtrados por categoría y otros criterios.
    """
    try:
        lat = request.args.get('lat', default=DEFAULT_LAT, type=float)
        lon = request.args.get('lon', default=DEFAULT_LON, type=float)
        rating = request.args.get('rating', default=0, type=float)
        
        user_location = (lat, lon)
        pois = fetch_pois(category)
        
        # Aplicar filtros
        filtered_pois = [poi for poi in pois if poi['rating'] >= rating]
        
        routes = calculate_routes(user_location, filtered_pois)
        return jsonify(routes)
    except Exception as e:
        logger.error(f"Error al obtener POIs: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/weather')
@error_handler
def get_weather():
    """
    Endpoint para obtener datos del clima.
    """
    lat = request.args.get('lat', default=DEFAULT_LAT, type=float)
    lon = request.args.get('lon', default=DEFAULT_LON, type=float)
    
    weather_data = fetch_weather(lat, lon)
    if weather_data:
        return jsonify(weather_data)
    else:
        return jsonify({"error": "No se pudo obtener la información del clima"}), 503

@app.route('/directions')
@error_handler
def get_directions():
    """
    Endpoint para obtener direcciones entre dos puntos.
    """
    start_lat = request.args.get('start_lat', type=float)
    start_lon = request.args.get('start_lon', type=float)
    end_lat = request.args.get('end_lat', type=float)
    end_lon = request.args.get('end_lon', type=float)

    if None in [start_lat, start_lon, end_lat, end_lon]:
        return jsonify({"error": "Faltan parámetros de ubicación"}), 400

    url = f"http://router.project-osrm.org/route/v1/driving/{start_lon},{start_lat};{end_lon},{end_lat}"
    params = {
        "overview": "full",
        "geometries": "geojson",
        "steps": "true"
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data["code"] == "Ok":
            route = data["routes"][0]
            return jsonify({
                "route": route["geometry"]["coordinates"],
                "distance": round(route["distance"] / 1000, 2),  # Convertir a km
                "duration": round(route["duration"] / 60)  # Convertir a minutos
            })
        else:
            return jsonify({"error": "No se pudo encontrar una ruta"}), 404
    except requests.RequestException as e:
        logger.error(f"Error al obtener direcciones: {e}")
        return jsonify({"error": "Error al obtener la ruta"}), 503

@app.errorhandler(404)
def not_found_error(error):
    return jsonify({"error": "Recurso no encontrado"}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Error interno del servidor: {error}")
    return jsonify({"error": "Error interno del servidor"}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
    
