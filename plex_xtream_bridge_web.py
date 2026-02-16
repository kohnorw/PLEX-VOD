#!/usr/bin/env python3
"""
Plex to Xtream Codes API Bridge with Web Interface
Allows Xtream UI players to access Plex library content with easy configuration
"""

from flask import Flask, jsonify, request, Response, render_template_string, redirect, url_for, session
from plexapi.server import PlexServer
import requests
import hashlib
import time
import json
import os
import signal
import sys
from datetime import datetime
from urllib.parse import quote
import secrets
import base64
from cryptography.fernet import Fernet
import threading
from queue import Queue, Empty

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))

# Background cache warming
cache_queue = Queue()
cache_warming_active = False
last_library_scan = 0
known_items = set()  # Track known movie/show IDs

def scan_for_new_content():
    """Periodically scan Plex for new content and cache it"""
    global last_library_scan, known_items
    
    if not plex or not TMDB_API_KEY:
        return
    
    current_time = time.time()
    
    # Only scan every 5 minutes
    if current_time - last_library_scan < 300:
        return
    
    last_library_scan = current_time
    
    try:
        new_items_found = 0
        
        # Scan movie libraries
        for section in plex.library.sections():
            if section.type == 'movie':
                for movie in section.recentlyAdded(maxresults=50):  # Check last 50 added
                    item_id = f"movie_{movie.ratingKey}"
                    
                    # New item detected
                    if item_id not in known_items:
                        known_items.add(item_id)
                        
                        # Check if already cached
                        cache_key = f"movie_{movie.ratingKey}"
                        if cache_key not in metadata_cache.get('movies', {}):
                            cache_queue.put(('movie', movie))
                            new_items_found += 1
                            print(f"[AUTO-CACHE] New movie detected: {movie.title}")
            
            elif section.type == 'show':
                for show in section.recentlyAdded(maxresults=50):  # Check last 50 added
                    item_id = f"show_{show.ratingKey}"
                    
                    # New item detected
                    if item_id not in known_items:
                        known_items.add(item_id)
                        
                        # Check if already cached (use 'series' for cache key)
                        cache_key = f"series_{show.ratingKey}"
                        if cache_key not in metadata_cache.get('series', {}):
                            # Queue as 'series' type for worker
                            cache_queue.put(('series', show))
                            new_items_found += 1
                            print(f"[AUTO-CACHE] New show detected: {show.title}")
        
        if new_items_found > 0:
            print(f"[AUTO-CACHE] Queued {new_items_found} new items for caching")
    
    except Exception as e:
        print(f"[AUTO-CACHE] Error scanning for new content: {e}")

def initialize_known_items():
    """Build initial set of known items from Plex"""
    global known_items
    
    if not plex:
        return
    
    try:
        for section in plex.library.sections():
            if section.type == 'movie':
                for movie in section.all():
                    known_items.add(f"movie_{movie.ratingKey}")
            elif section.type == 'show':
                for show in section.all():
                    known_items.add(f"show_{show.ratingKey}")
        
        print(f"[AUTO-CACHE] Initialized tracking for {len(known_items)} items")
    except Exception as e:
        print(f"[AUTO-CACHE] Error initializing known items: {e}")

def cache_worker():
    """Background worker to pre-cache TMDb metadata"""
    global cache_warming_active
    print("[CACHE] Cache warming worker started")
    
    items_processed = 0
    items_failed = 0
    last_save = time.time()
    consecutive_timeouts = 0
    
    while cache_warming_active:
        try:
            # Get item from queue with timeout
            try:
                item = cache_queue.get(timeout=5)  # Increased timeout to 5 seconds
                consecutive_timeouts = 0  # Reset on successful get
            except Empty:
                # Queue is empty, this is normal
                consecutive_timeouts += 1
                if consecutive_timeouts > 12:  # 1 minute of empty queue (12 * 5 seconds)
                    print(f"[CACHE] Queue empty for 1 minute, worker pausing...")
                    time.sleep(30)  # Sleep for 30 seconds
                    consecutive_timeouts = 0
                continue
            
            if item is None:  # Poison pill to stop worker
                print("[CACHE] Received stop signal")
                break
            
            item_type, plex_item = item
            
            # Validate item
            if not plex_item or not hasattr(plex_item, 'ratingKey'):
                print(f"[CACHE] Invalid item in queue: {item}")
                cache_queue.task_done()
                continue
            
            cache_key = f"{item_type}_{plex_item.ratingKey}"
            
            # Skip if already cached
            if cache_key in metadata_cache.get(item_type + 's', {}):
                print(f"[CACHE] Skipping (already cached): {plex_item.title if hasattr(plex_item, 'title') else cache_key}")
                cache_queue.task_done()
                continue
            
            # Fetch TMDb data
            try:
                if item_type == 'movie':
                    tmdb_data = enhance_movie_with_tmdb(plex_item)
                elif item_type == 'series':
                    tmdb_data = enhance_series_with_tmdb(plex_item)
                else:
                    print(f"[CACHE] Unknown item type: {item_type}")
                    items_failed += 1
                    cache_queue.task_done()
                    continue
                
                if tmdb_data:
                    # Ensure cache structure exists
                    cache_category = item_type + 's'  # 'movies' or 'seriess' - WAIT, THIS IS WRONG!
                    
                    # Fix: 'series' + 's' = 'seriess' which is wrong!
                    # Should be: 'movie' -> 'movies', 'series' -> 'series' (already plural)
                    if item_type == 'movie':
                        cache_category = 'movies'
                    elif item_type == 'series':
                        cache_category = 'series'  # Don't add 's'
                    else:
                        cache_category = item_type + 's'
                    
                    if cache_category not in metadata_cache:
                        metadata_cache[cache_category] = {}
                    
                    metadata_cache[cache_category][cache_key] = tmdb_data
                    items_processed += 1
                    
                    # Debug: Show what we just saved
                    if item_type == 'series':
                        current_series_count = len(metadata_cache.get('series', {}))
                        print(f"[CACHE] ✓ Cached {item_type}: {plex_item.title if hasattr(plex_item, 'title') else cache_key} (Total series now: {current_series_count})")
                    else:
                        print(f"[CACHE] ✓ Cached {item_type}: {plex_item.title if hasattr(plex_item, 'title') else cache_key}")
                    
                    # Progress update every 10 items
                    if items_processed % 10 == 0:
                        remaining = cache_queue.qsize()
                        movies = len(metadata_cache.get('movies', {}))
                        series = len(metadata_cache.get('series', {}))
                        print(f"[CACHE] Progress: {items_processed} cached, {items_failed} failed, {remaining} in queue (Movies: {movies}, Shows: {series})")
                else:
                    items_failed += 1
                    print(f"[CACHE] ✗ No TMDb data for {item_type}: {plex_item.title if hasattr(plex_item, 'title') else cache_key}")
            except Exception as e:
                items_failed += 1
                print(f"[CACHE] ✗ Error caching {plex_item.title if hasattr(plex_item, 'title') else 'item'}: {e}")
                import traceback
                traceback.print_exc()
            
            cache_queue.task_done()
            
            # Save to disk every 50 items or every 5 minutes
            current_time = time.time()
            if items_processed % 50 == 0 or (current_time - last_save) > 300:
                save_cache_to_disk()
                last_save = current_time
            
            time.sleep(0.5)  # Rate limiting - 2 requests per second
            
        except Exception as e:
            print(f"[CACHE] Worker exception: {e}")
            import traceback
            traceback.print_exc()
            try:
                cache_queue.task_done()
            except:
                pass
            time.sleep(1)  # Wait a bit before continuing
            continue
    
    # Final save when worker stops
    if items_processed > 0:
        save_cache_to_disk()
    
    print(f"[CACHE] Cache warming worker stopped")
    print(f"[CACHE] Stats: {items_processed} cached, {items_failed} failed")
    print(f"[CACHE] Final cache size: {len(metadata_cache.get('movies', {}))} movies, {len(metadata_cache.get('series', {}))} shows")

def start_cache_warming():
    """Start the background cache warming thread"""
    global cache_warming_active
    
    if cache_warming_active:
        return
    
    cache_warming_active = True
    worker_thread = threading.Thread(target=cache_worker, daemon=True)
    worker_thread.start()
    print("[CACHE] Background cache warming enabled")

def warm_cache_for_library(section_type='movie', limit=None):
    """Queue items from library for background caching"""
    if not TMDB_API_KEY:
        print("[CACHE] TMDb API key not set, skipping cache warming")
        return
    
    try:
        # Map section_type to Plex type and cache type
        plex_type = section_type if section_type == 'movie' else 'show'
        cache_type = section_type if section_type == 'movie' else 'series'
        
        count = 0
        for section in plex.library.sections():
            if section.type == plex_type:
                items = section.all()
                print(f"[CACHE] Found {len(items)} {plex_type}s in section '{section.title}'")
                
                for item in items:
                    if limit and count >= limit:
                        break
                    
                    # Check if already cached (use correct cache type)
                    cache_key = f"{cache_type}_{item.ratingKey}"
                    if cache_key not in metadata_cache.get(cache_type + 's', {}):
                        # Queue with correct type for worker
                        cache_queue.put((cache_type, item))
                        count += 1
                
                if limit and count >= limit:
                    break
        
        print(f"[CACHE] Queued {count} {plex_type}s for background caching")
    except Exception as e:
        print(f"[CACHE] Error warming cache: {e}")
        import traceback
        traceback.print_exc()

# Configuration - Update these with your settings
PLEX_URL = os.getenv('PLEX_URL', '')
PLEX_TOKEN = os.getenv('PLEX_TOKEN', '')
BRIDGE_USERNAME = os.getenv('BRIDGE_USERNAME', 'admin')
BRIDGE_PASSWORD = os.getenv('BRIDGE_PASSWORD', 'admin')
BRIDGE_HOST = os.getenv('BRIDGE_HOST', '0.0.0.0')
BRIDGE_PORT = int(os.getenv('BRIDGE_PORT', '8080'))
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin123')  # Web interface password
SHOW_DUMMY_CHANNEL = os.getenv('SHOW_DUMMY_CHANNEL', 'true').lower() == 'true'  # Show info channel
TMDB_API_KEY = os.getenv('TMDB_API_KEY', '')  # TMDb API key for metadata

# Content limits (can be overridden via environment variables)
MAX_MOVIES = int(os.getenv('MAX_MOVIES', '10000'))  # Maximum movies to return
MAX_SHOWS = int(os.getenv('MAX_SHOWS', '5000'))     # Maximum TV shows to return

# Configuration file paths - all in data directory
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
CATEGORIES_FILE = os.path.join(DATA_DIR, 'categories.json')
ENCRYPTION_KEY_FILE = os.path.join(DATA_DIR, '.encryption_key')
CACHE_FILE = os.path.join(DATA_DIR, 'tmdb_cache.json')

# Initialize Plex connection
plex = None

# Custom categories cache
custom_categories = {
    'movies': [],
    'series': []
}

# Metadata cache for faster loading
# Simple in-memory cache (per-session, not saved to disk)
session_cache = {
    'movies': {},
    'series': {},
    'categories': {},
    'sections': None,
    'sections_time': 0
}

# Metadata cache for faster loading
# Simple in-memory cache (per-session, not saved to disk)

def save_cache_to_disk():
    """Save TMDb cache to disk"""
    try:
        # Ensure data directory exists
        data_dir = os.path.dirname(CACHE_FILE)
        os.makedirs(data_dir, exist_ok=True)
        
        cache_data = {
            'movies': session_cache['movies'],
            'series': session_cache['series']
        }
        
        # Write to temp file first, then rename (atomic operation)
        temp_file = CACHE_FILE + '.tmp'
        with open(temp_file, 'w') as f:
            json.dump(cache_data, f, indent=2)
        
        # Rename temp file to actual file
        os.replace(temp_file, CACHE_FILE)
        
        print(f"[CACHE] Saved {len(cache_data['movies'])} movies and {len(cache_data['series'])} shows to {CACHE_FILE}")
        return True
    except Exception as e:
        print(f"[CACHE] Error saving cache: {e}")
        import traceback
        traceback.print_exc()
        return False

def load_cache_from_disk():
    """Load TMDb cache from disk"""
    try:
        if os.path.exists(CACHE_FILE):
            print(f"[CACHE] Loading cache from {CACHE_FILE}")
            with open(CACHE_FILE, 'r') as f:
                cache_data = json.load(f)
            
            session_cache['movies'] = cache_data.get('movies', {})
            session_cache['series'] = cache_data.get('series', {})
            
            print(f"[CACHE] ✓ Loaded {len(session_cache['movies'])} movies and {len(session_cache['series'])} shows from disk")
            return True
        else:
            print(f"[CACHE] No cache file found at {CACHE_FILE}, starting fresh")
            return False
    except Exception as e:
        print(f"[CACHE] Error loading cache: {e}")
        import traceback
        traceback.print_exc()
        return False

# Auto-matching state
auto_matching_running = False
last_auto_match_time = 0

def auto_match_content():
    """Background task to auto-match unmatched content with TMDb"""
    global auto_matching_running, last_auto_match_time
    
    if not TMDB_API_KEY or not plex:
        return
    
    auto_matching_running = True
    matched_count = 0
    
    try:
        print("[AUTO-MATCH] Starting auto-match scan...")
        
        # Match movies
        for section in plex.library.sections():
            if section.type == 'movie':
                for movie in section.all():
                    cache_key = f"movie_{movie.ratingKey}"
                    if cache_key not in session_cache['movies']:
                        # Try to match with TMDb
                        tmdb_data = enhance_movie_with_tmdb(movie)
                        if tmdb_data:
                            session_cache['movies'][cache_key] = tmdb_data
                            matched_count += 1
                            print(f"[AUTO-MATCH] Matched movie: {movie.title}")
            
            elif section.type == 'show':
                for show in section.all():
                    cache_key = f"series_{show.ratingKey}"
                    if cache_key not in session_cache['series']:
                        # Try to match with TMDb
                        tmdb_data = enhance_series_with_tmdb(show)
                        if tmdb_data:
                            session_cache['series'][cache_key] = tmdb_data
                            matched_count += 1
                            print(f"[AUTO-MATCH] Matched show: {show.title}")
        
        print(f"[AUTO-MATCH] Completed! Matched {matched_count} items")
        last_auto_match_time = time.time()
        
        # Save cache to disk
        if matched_count > 0:
            save_cache_to_disk()
    
    except Exception as e:
        print(f"[AUTO-MATCH] Error: {e}")
    
    finally:
        auto_matching_running = False

def background_auto_matcher():
    """Background thread that runs auto-matching on startup and every 30 minutes"""
    print("[AUTO-MATCH] Background auto-matcher started")
    
    # Run immediately on startup
    print("[AUTO-MATCH] Running initial auto-match on startup...")
    auto_match_content()
    
    # Then run every 30 minutes
    while True:
        time.sleep(1800)  # 30 minutes = 1800 seconds
        
        if not auto_matching_running:
            print("[AUTO-MATCH] Running scheduled auto-match...")
            auto_match_content()

# Cache library sections (updated every 5 minutes)
def get_cached_sections():
    """Get library sections with caching to reduce Plex API calls"""
    current_time = time.time()
    
    if session_cache['sections'] and (current_time - session_cache['sections_time']) < 300:
        return session_cache['sections']
    
    # Refresh sections
    if plex:
        session_cache['sections'] = list(plex.library.sections())
        session_cache['sections_time'] = current_time
    
    return session_cache['sections']

# Track known items to detect new content
known_item_ids = set()
last_scan_time = 0

def scan_for_new_plex_content():
    """Scan Plex for new content and pre-cache TMDb data"""
    global known_item_ids, last_scan_time
    
    if not plex or not TMDB_API_KEY:
        return
    
    try:
        new_items = 0
        
        # Scan all sections
        for section in plex.library.sections():
            if section.type == 'movie':
                for movie in section.all():
                    item_id = f"movie_{movie.ratingKey}"
                    
                    # New movie detected
                    if item_id not in known_item_ids:
                        known_item_ids.add(item_id)
                        
                        # Pre-cache TMDb data
                        cache_key = f"movie_{movie.ratingKey}"
                        if cache_key not in session_cache['movies']:
                            tmdb_data = enhance_movie_with_tmdb(movie)
                            if tmdb_data:
                                session_cache['movies'][cache_key] = tmdb_data
                                new_items += 1
                                print(f"[NEW] Cached new movie: {movie.title}")
            
            elif section.type == 'show':
                for show in section.all():
                    item_id = f"show_{show.ratingKey}"
                    
                    # New show detected
                    if item_id not in known_item_ids:
                        known_item_ids.add(item_id)
                        
                        # Pre-cache TMDb data
                        cache_key = f"series_{show.ratingKey}"
                        if cache_key not in session_cache['series']:
                            tmdb_data = enhance_series_with_tmdb(show)
                            if tmdb_data:
                                session_cache['series'][cache_key] = tmdb_data
                                new_items += 1
                                print(f"[NEW] Cached new show: {show.title}")
        
        if new_items > 0:
            print(f"[SCAN] Found and cached {new_items} new items")
        
        last_scan_time = time.time()
    
    except Exception as e:
        print(f"[SCAN] Error scanning for new content: {e}")

def background_scanner():
    """Background thread that scans every 15 minutes"""
    global last_scan_time
    
    print("[SCAN] Background scanner started - checking every 15 minutes")
    
    # Don't scan on startup, wait for first interval
    last_scan_time = time.time()
    
    while True:
        time.sleep(900)  # 15 minutes = 900 seconds
        
        print("[SCAN] Running 15-minute scan for new content...")
        scan_for_new_plex_content()

def get_poster_url(item, tmdb_data=None):
    """Get best available poster URL"""
    # Priority: TMDb high-res > Plex
    if tmdb_data and tmdb_data.get('poster_path'):
        return tmdb_data['poster_path']
    
    # Fallback to Plex
    if hasattr(item, 'thumb') and item.thumb:
        return f"{PLEX_URL}{item.thumb}?X-Plex-Token={PLEX_TOKEN}"
    
    return ""

def get_backdrop_url(item, tmdb_data=None):
    """Get best available backdrop URL"""
    # Priority: TMDb high-res > Plex
    if tmdb_data and tmdb_data.get('backdrop_path'):
        return tmdb_data['backdrop_path']
    
    # Fallback to Plex
    if hasattr(item, 'art') and item.art:
        return f"{PLEX_URL}{item.art}?X-Plex-Token={PLEX_TOKEN}"
    
    return ""

def clear_metadata_cache():
    """Clear the metadata cache"""
    global metadata_cache
    metadata_cache = {
        'movies': {},
        'series': {},
        'last_refresh': 0
    }
    print("✓ Metadata cache cleared")

# Encryption setup
_fernet = None

def get_encryption_key():
    """Get or create encryption key"""
    global _fernet
    
    if _fernet is not None:
        return _fernet
    
    # Check if key file exists
    if os.path.exists(ENCRYPTION_KEY_FILE):
        with open(ENCRYPTION_KEY_FILE, 'rb') as f:
            key = f.read()
    else:
        # Generate new key
        key = Fernet.generate_key()
        # Save key securely
        with open(ENCRYPTION_KEY_FILE, 'wb') as f:
            f.write(key)
        # Set restrictive permissions
        os.chmod(ENCRYPTION_KEY_FILE, 0o600)
    
    _fernet = Fernet(key)
    return _fernet

def encrypt_value(value):
    """Encrypt a sensitive value"""
    if not value:
        return ""
    
    try:
        fernet = get_encryption_key()
        encrypted = fernet.encrypt(value.encode())
        return base64.b64encode(encrypted).decode()
    except Exception as e:
        print(f"✗ Encryption error: {e}")
        return value

def decrypt_value(encrypted_value):
    """Decrypt a sensitive value"""
    if not encrypted_value:
        return ""
    
    try:
        fernet = get_encryption_key()
        decoded = base64.b64decode(encrypted_value.encode())
        decrypted = fernet.decrypt(decoded)
        return decrypted.decode()
    except Exception as e:
        print(f"✗ Decryption error: {e}")
        return encrypted_value

def hash_password(password):
    """Hash a password for storage"""
    return hashlib.sha256(password.encode()).hexdigest()

def check_first_login():
    """Check if this is the first login (default password still in use)"""
    # Check admin password
    if ADMIN_PASSWORD == 'admin123':
        return True
    
    # Check Xtream credentials
    if BRIDGE_USERNAME == 'admin' and BRIDGE_PASSWORD == 'admin':
        return True
    
    # Also check if config exists with defaults
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                stored_pass = config.get('admin_password', '')
                stored_user = config.get('bridge_username', '')
                stored_bridge = config.get('bridge_password', '')
                
                # Check if admin password is default or hash of default
                if stored_pass == 'admin123' or stored_pass == hash_password('admin123'):
                    return True
                
                # Check if Xtream credentials are default
                if stored_user == 'admin' and stored_bridge == 'admin':
                    return True
        except:
            pass
    
    return False

# TMDb API functions
def fetch_tmdb_data(title, year=None, media_type='movie'):
    """Fetch metadata from TMDb"""
    if not TMDB_API_KEY:
        return None
    
    try:
        import requests
        
        # Search for the item
        search_url = f"https://api.themoviedb.org/3/search/{media_type}"
        params = {
            'api_key': TMDB_API_KEY,
            'query': title,
            'language': 'en-US'
        }
        
        if year and media_type == 'movie':
            params['year'] = year
        elif year and media_type == 'tv':
            params['first_air_date_year'] = year
        
        response = requests.get(search_url, params=params, timeout=5)
        
        if response.status_code == 200:
            results = response.json().get('results', [])
            if results:
                # Get the first result
                item = results[0]
                
                # Fetch detailed info
                detail_url = f"https://api.themoviedb.org/3/{media_type}/{item['id']}"
                detail_params = {
                    'api_key': TMDB_API_KEY,
                    'language': 'en-US',
                    'append_to_response': 'credits,keywords,videos'
                }
                
                detail_response = requests.get(detail_url, params=detail_params, timeout=5)
                
                if detail_response.status_code == 200:
                    return detail_response.json()
        
        return None
    except Exception as e:
        print(f"Error fetching TMDb data: {e}")
        return None

def enhance_movie_with_tmdb(movie):
    """Enhance movie data with TMDb metadata"""
    if not TMDB_API_KEY:
        return {}
    
    try:
        # Get year from Plex
        year = movie.year if hasattr(movie, 'year') else None
        
        # Fetch TMDb data
        tmdb_data = fetch_tmdb_data(movie.title, year, 'movie')
        
        if tmdb_data:
            enhanced = {
                'tmdb_id': tmdb_data.get('id'),
                'imdb_id': tmdb_data.get('imdb_id', ''),
                'overview': tmdb_data.get('overview', ''),
                'tagline': tmdb_data.get('tagline', ''),
                'popularity': tmdb_data.get('popularity', 0),
                'vote_average': tmdb_data.get('vote_average', 0),
                'vote_count': tmdb_data.get('vote_count', 0),
                'backdrop_path': f"https://image.tmdb.org/t/p/original{tmdb_data.get('backdrop_path')}" if tmdb_data.get('backdrop_path') else '',
                'poster_path': f"https://image.tmdb.org/t/p/original{tmdb_data.get('poster_path')}" if tmdb_data.get('poster_path') else '',
                'genres': [g['name'] for g in tmdb_data.get('genres', [])],
                'keywords': [k['name'] for k in tmdb_data.get('keywords', {}).get('keywords', [])],
                'cast': [{'name': c['name'], 'character': c['character']} for c in tmdb_data.get('credits', {}).get('cast', [])[:10]],
                'director': next((c['name'] for c in tmdb_data.get('credits', {}).get('crew', []) if c['job'] == 'Director'), ''),
                'trailer': next((f"https://www.youtube.com/watch?v={v['key']}" for v in tmdb_data.get('videos', {}).get('results', []) if v['site'] == 'YouTube' and v['type'] == 'Trailer'), '')
            }
            return enhanced
    except Exception as e:
        print(f"Error enhancing movie with TMDb: {e}")
    
    return {}

def enhance_series_with_tmdb(show):
    """Enhance TV show data with TMDb metadata"""
    if not TMDB_API_KEY:
        return {}
    
    try:
        # Safely get show title
        title = show.title if hasattr(show, 'title') else 'Unknown'
        
        # Get year from Plex
        year = show.year if hasattr(show, 'year') else None
        
        # Fetch TMDb data
        tmdb_data = fetch_tmdb_data(title, year, 'tv')
        
        if tmdb_data:
            enhanced = {
                'tmdb_id': tmdb_data.get('id'),
                'overview': tmdb_data.get('overview', ''),
                'popularity': tmdb_data.get('popularity', 0),
                'vote_average': tmdb_data.get('vote_average', 0),
                'vote_count': tmdb_data.get('vote_count', 0),
                'backdrop_path': f"https://image.tmdb.org/t/p/original{tmdb_data.get('backdrop_path')}" if tmdb_data.get('backdrop_path') else '',
                'poster_path': f"https://image.tmdb.org/t/p/original{tmdb_data.get('poster_path')}" if tmdb_data.get('poster_path') else '',
                'genres': [g['name'] for g in tmdb_data.get('genres', [])],
                'keywords': [k['name'] for k in tmdb_data.get('keywords', {}).get('results', [])] if tmdb_data.get('keywords') else [],
                'cast': [{'name': c['name'], 'character': c['character']} for c in tmdb_data.get('credits', {}).get('cast', [])[:10]] if tmdb_data.get('credits') else [],
                'created_by': [c['name'] for c in tmdb_data.get('created_by', [])] if tmdb_data.get('created_by') else [],
                'networks': [n['name'] for n in tmdb_data.get('networks', [])] if tmdb_data.get('networks') else [],
                'number_of_seasons': tmdb_data.get('number_of_seasons', 0),
                'number_of_episodes': tmdb_data.get('number_of_episodes', 0),
                'status': tmdb_data.get('status', ''),
                'trailer': next((f"https://www.youtube.com/watch?v={v['key']}" for v in tmdb_data.get('videos', {}).get('results', []) if v.get('site') == 'YouTube' and v.get('type') == 'Trailer'), '') if tmdb_data.get('videos') else ''
            }
            return enhanced
    except Exception as e:
        print(f"[ERROR] Error enhancing series '{show.title if hasattr(show, 'title') else 'Unknown'}' with TMDb: {e}")
        import traceback
        traceback.print_exc()
    
    return {}

def load_config():
    """Load configuration from file"""
    global PLEX_URL, PLEX_TOKEN, BRIDGE_USERNAME, BRIDGE_PASSWORD, ADMIN_PASSWORD, SHOW_DUMMY_CHANNEL, TMDB_API_KEY, custom_categories
    
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
                PLEX_URL = config.get('plex_url', PLEX_URL)
                
                # Decrypt sensitive values
                encrypted_token = config.get('plex_token', PLEX_TOKEN)
                PLEX_TOKEN = decrypt_value(encrypted_token) if encrypted_token else PLEX_TOKEN
                
                BRIDGE_USERNAME = config.get('bridge_username', BRIDGE_USERNAME)
                BRIDGE_PASSWORD = config.get('bridge_password', BRIDGE_PASSWORD)
                ADMIN_PASSWORD = config.get('admin_password', ADMIN_PASSWORD)
                SHOW_DUMMY_CHANNEL = config.get('show_dummy_channel', SHOW_DUMMY_CHANNEL)
                
                encrypted_tmdb = config.get('tmdb_api_key', TMDB_API_KEY)
                TMDB_API_KEY = decrypt_value(encrypted_tmdb) if encrypted_tmdb else TMDB_API_KEY
                
                print("✓ Configuration loaded from file")
        except Exception as e:
            print(f"✗ Error loading config: {e}")
    
    # Load custom categories
    if os.path.exists(CATEGORIES_FILE):
        try:
            with open(CATEGORIES_FILE, 'r') as f:
                custom_categories = json.load(f)
                print(f"✓ Loaded {len(custom_categories.get('movies', []))} movie categories and {len(custom_categories.get('series', []))} series categories")
        except Exception as e:
            print(f"✗ Error loading categories: {e}")

def save_config():
    """Save configuration to file"""
    config = {
        'plex_url': PLEX_URL,
        'plex_token': encrypt_value(PLEX_TOKEN),  # Encrypt token
        'bridge_username': BRIDGE_USERNAME,
        'bridge_password': BRIDGE_PASSWORD,
        'admin_password': hash_password(ADMIN_PASSWORD),  # Hash password
        'show_dummy_channel': SHOW_DUMMY_CHANNEL,
        'tmdb_api_key': encrypt_value(TMDB_API_KEY)  # Encrypt API key
    }
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        # Set restrictive permissions
        os.chmod(CONFIG_FILE, 0o600)
        print("✓ Configuration saved to file (sensitive data encrypted)")
        return True
    except Exception as e:
        print(f"✗ Error saving config: {e}")
        return False

def save_categories():
    """Save custom categories to file"""
    try:
        with open(CATEGORIES_FILE, 'w') as f:
            json.dump(custom_categories, f, indent=2)
        print("✓ Categories saved to file")
        return True
    except Exception as e:
        print(f"✗ Error saving categories: {e}")
        return False

def connect_plex():
    """Connect to Plex server"""
    global plex
    if PLEX_URL and PLEX_TOKEN:
        try:
            plex = PlexServer(PLEX_URL, PLEX_TOKEN)
            print(f"✓ Connected to Plex Server: {plex.friendlyName}")
            return True
        except Exception as e:
            print(f"✗ Failed to connect to Plex: {e}")
            plex = None
            return False
    return False

def get_smart_categories_for_movies():
    """Generate smart categories for movies using Plex metadata"""
    categories = []
    base_id = 10000
    
    if not plex:
        return categories
    
    try:
        # Get all movie sections
        movie_sections = [s for s in plex.library.sections() if s.type == 'movie']
        
        for section in movie_sections:
            # Get Plex's native filters and collections
            
            # Recently Added (using Plex's method)
            categories.append({
                'id': f"{base_id}",
                'name': f"🆕 Recently Added - {section.title}",
                'type': 'plex_recently_added',
                'section_id': section.key,
                'limit': 50
            })
            base_id += 1
            
            # Get all unique genres from the section
            try:
                genres = set()
                for movie in section.search(limit=500):  # Sample more for better genre detection
                    if movie.genres:
                        for genre in movie.genres:
                            genres.add(genre.tag)
                
                # Create a category for each genre using Plex's genre filter
                for genre in sorted(genres):
                    categories.append({
                        'id': f"{base_id}",
                        'name': f"🎭 {genre} - {section.title}",
                        'type': 'plex_genre',
                        'section_id': section.key,
                        'genre': genre,
                        'limit': 200
                    })
                    base_id += 1
            except Exception as e:
                print(f"Error getting genres: {e}")
            
            # Get all unique decades
            try:
                decades = set()
                for movie in section.search(limit=500):
                    if movie.year:
                        decade = (movie.year // 10) * 10
                        decades.add(decade)
                
                # Create decade categories
                for decade in sorted(decades, reverse=True):
                    if decade >= 1920:  # Only decades from 1920 onwards
                        categories.append({
                            'id': f"{base_id}",
                            'name': f"📅 {decade}s - {section.title}",
                            'type': 'plex_decade',
                            'section_id': section.key,
                            'decade': decade,
                            'limit': 200
                        })
                        base_id += 1
            except Exception as e:
                print(f"Error getting decades: {e}")
            
            # Get Plex Collections (if any)
            try:
                collections = section.collections()
                for collection in collections:
                    categories.append({
                        'id': f"{base_id}",
                        'name': f"📚 {collection.title}",
                        'type': 'plex_collection',
                        'section_id': section.key,
                        'collection_id': collection.ratingKey,
                        'limit': 200
                    })
                    base_id += 1
            except Exception as e:
                print(f"Error getting collections: {e}")
        
    except Exception as e:
        print(f"Error generating movie categories: {e}")
    
    return categories

def get_smart_categories_for_series():
    """Generate smart categories for TV shows using Plex metadata"""
    categories = []
    base_id = 20000
    
    if not plex:
        return categories
    
    try:
        # Get all TV sections
        tv_sections = [s for s in plex.library.sections() if s.type == 'show']
        
        for section in tv_sections:
            # Recently Added (using Plex's method)
            categories.append({
                'id': f"{base_id}",
                'name': f"🆕 Recently Added - {section.title}",
                'type': 'plex_recently_added',
                'section_id': section.key,
                'limit': 50
            })
            base_id += 1
            
            # Get all unique genres
            try:
                genres = set()
                for show in section.search(limit=500):
                    if show.genres:
                        for genre in show.genres:
                            genres.add(genre.tag)
                
                # Create genre categories using Plex's genre filter
                for genre in sorted(genres):
                    categories.append({
                        'id': f"{base_id}",
                        'name': f"🎭 {genre} - {section.title}",
                        'type': 'plex_genre',
                        'section_id': section.key,
                        'genre': genre,
                        'limit': 200
                    })
                    base_id += 1
            except Exception as e:
                print(f"Error getting genres: {e}")
            
            # Get all unique decades
            try:
                decades = set()
                for show in section.search(limit=500):
                    if show.year:
                        decade = (show.year // 10) * 10
                        decades.add(decade)
                
                # Create decade categories
                for decade in sorted(decades, reverse=True):
                    if decade >= 1950:  # Only decades from 1950 onwards for TV
                        categories.append({
                            'id': f"{base_id}",
                            'name': f"📅 {decade}s - {section.title}",
                            'type': 'plex_decade',
                            'section_id': section.key,
                            'decade': decade,
                            'limit': 200
                        })
                        base_id += 1
            except Exception as e:
                print(f"Error getting decades: {e}")
            
            # Get Plex Collections
            try:
                collections = section.collections()
                for collection in collections:
                    categories.append({
                        'id': f"{base_id}",
                        'name': f"📚 {collection.title}",
                        'type': 'plex_collection',
                        'section_id': section.key,
                        'collection_id': collection.ratingKey,
                        'limit': 200
                    })
                    base_id += 1
            except Exception as e:
                print(f"Error getting collections: {e}")
        
    except Exception as e:
        print(f"Error generating series categories: {e}")
    
    return categories

def get_movies_for_category(category):
    """Get movies for a specific category using Plex filters"""
    movies = []
    
    if not plex:
        return movies
    
    try:
        section = plex.library.sectionByID(int(category['section_id']))
        
        if category['type'] == 'plex_recently_added':
            # Use Plex's native recently added
            items = section.recentlyAdded(maxresults=category.get('limit', 50))
        
        elif category['type'] == 'plex_genre':
            # Use Plex's genre filter
            items = section.search(genre=category['genre'], limit=category.get('limit', 200))
        
        elif category['type'] == 'plex_decade':
            # Filter by decade
            decade = category['decade']
            items = section.search(
                **{'year>>': decade, 'year<<': decade + 9},
                limit=category.get('limit', 200)
            )
        
        elif category['type'] == 'plex_collection':
            # Get items from Plex collection
            try:
                collection = plex.fetchItem(int(category['collection_id']))
                items = collection.items()[:category.get('limit', 200)]
            except:
                items = []
        
        elif category['type'] == 'custom':
            # Custom filter
            items = section.search(**category.get('filters', {}), limit=category.get('limit', 50))
        
        else:
            items = []
        
        for movie in items:
            formatted = format_movie_for_xtream(movie, category['id'])
            if formatted:
                movies.append(formatted)
    
    except Exception as e:
        print(f"Error getting movies for category: {e}")
    
    return movies

def get_series_for_category(category):
    """Get TV shows for a specific category using Plex filters"""
    series = []
    
    if not plex:
        return series
    
    try:
        section = plex.library.sectionByID(int(category['section_id']))
        
        if category['type'] == 'plex_recently_added':
            # Use Plex's native recently added
            items = section.recentlyAdded(maxresults=category.get('limit', 50))
        
        elif category['type'] == 'plex_genre':
            # Use Plex's genre filter
            items = section.search(genre=category['genre'], limit=category.get('limit', 200))
        
        elif category['type'] == 'plex_decade':
            # Filter by decade
            decade = category['decade']
            items = section.search(
                **{'year>>': decade, 'year<<': decade + 9},
                limit=category.get('limit', 200)
            )
        
        elif category['type'] == 'plex_collection':
            # Get items from Plex collection
            try:
                collection = plex.fetchItem(int(category['collection_id']))
                items = collection.items()[:category.get('limit', 200)]
            except:
                items = []
        
        elif category['type'] == 'custom':
            items = section.search(**category.get('filters', {}), limit=category.get('limit', 50))
        
        else:
            items = []
        
        for show in items:
            formatted = format_series_for_xtream(show, category['id'])
            if formatted:
                series.append(formatted)
    
    except Exception as e:
        print(f"Error getting series for category: {e}")
    
    return series

# Load config and connect on startup
load_config()
connect_plex()

# Session storage
sessions = {}
active_streams = {}  # Track active streaming sessions

def authenticate(username, password):
    """Authenticate user credentials"""
    return username == BRIDGE_USERNAME and password == BRIDGE_PASSWORD

def create_session(username):
    """Create a session token"""
    session_id = hashlib.md5(f"{username}{time.time()}".encode()).hexdigest()
    sessions[session_id] = {
        'username': username,
        'created_at': time.time()
    }
    return session_id

def track_stream_start(username, stream_id, stream_type):
    """Track when a user starts streaming"""
    stream_key = f"{username}_{stream_id}"
    active_streams[stream_key] = {
        'username': username,
        'stream_id': stream_id,
        'stream_type': stream_type,
        'started_at': time.time(),
        'last_active': time.time()
    }
    cleanup_inactive_streams()

def cleanup_inactive_streams():
    """Remove streams inactive for more than 5 minutes"""
    current_time = time.time()
    inactive_threshold = 300  # 5 minutes
    
    to_remove = []
    for key, stream in active_streams.items():
        if current_time - stream['last_active'] > inactive_threshold:
            to_remove.append(key)
    
    for key in to_remove:
        del active_streams[key]

def get_active_user_count():
    """Get count of unique active users"""
    cleanup_inactive_streams()
    unique_users = set(stream['username'] for stream in active_streams.values())
    return len(unique_users)

def validate_session():
    """Validate session from request parameters"""
    username = request.args.get('username')
    password = request.args.get('password')
    
    if username and password:
        return authenticate(username, password)
    return False

def get_stream_url(item, session_info=""):
    """Generate stream URL for Plex item"""
    media = item.media[0] if item.media else None
    if not media:
        return None
    
    part = media.parts[0] if media.parts else None
    if not part:
        return None
    
    base_url = PLEX_URL.rstrip('/')
    # Use direct file access - this works reliably
    stream_url = f"{base_url}{part.key}?X-Plex-Token={PLEX_TOKEN}"
    
    return stream_url

def format_movie_for_xtream(movie, category_id=1, skip_tmdb=False):
    """Format Plex movie to Xtream Codes format - with TMDb posters"""
    try:
        stream_url = get_stream_url(movie)
        if not stream_url:
            return None
        
        # Get TMDb data for high-quality posters (with caching)
        tmdb_data = None
        if TMDB_API_KEY and not skip_tmdb:
            cache_key = f"movie_{movie.ratingKey}"
            
            # Check cache first
            if cache_key in session_cache['movies']:
                tmdb_data = session_cache['movies'][cache_key]
            else:
                # Fetch and cache
                tmdb_data = enhance_movie_with_tmdb(movie)
                if tmdb_data:
                    session_cache['movies'][cache_key] = tmdb_data
        
        # Use TMDb posters if available, otherwise Plex
        if tmdb_data and tmdb_data.get('poster_path'):
            poster_url = tmdb_data['poster_path']
        else:
            try:
                poster_url = movie.thumbUrl if hasattr(movie, 'thumbUrl') else ""
                if not poster_url and hasattr(movie, 'thumb'):
                    poster_url = f"{PLEX_URL}{movie.thumb}?X-Plex-Token={PLEX_TOKEN}"
            except:
                poster_url = ""
        
        # Use TMDb backdrop if available, otherwise Plex
        if tmdb_data and tmdb_data.get('backdrop_path'):
            backdrop_url = tmdb_data['backdrop_path']
        else:
            try:
                backdrop_url = movie.artUrl if hasattr(movie, 'artUrl') else ""
                if not backdrop_url and hasattr(movie, 'art'):
                    backdrop_url = f"{PLEX_URL}{movie.art}?X-Plex-Token={PLEX_TOKEN}"
            except:
                backdrop_url = ""
        
        # Format with TMDb posters - multiple field names for compatibility
        formatted = {
            "stream_id": movie.ratingKey,
            "num": movie.ratingKey,
            "name": movie.title,
            # Multiple poster field names for different apps
            "stream_icon": poster_url,
            "icon": poster_url,
            "cover": poster_url,
            "poster": poster_url,
            "image": poster_url,
            # Backdrop fields
            "cover_big": backdrop_url,
            "backdrop": backdrop_url,
            "fanart": backdrop_url,
            "added": str(int(movie.addedAt.timestamp())) if hasattr(movie, 'addedAt') and movie.addedAt else "",
            "category_id": str(category_id),
            "container_extension": "mkv",
            "direct_source": stream_url,
            # Year for better matching
            "year": str(movie.year) if hasattr(movie, 'year') and movie.year else "",
            "releaseDate": str(movie.year) if hasattr(movie, 'year') and movie.year else "",
            # TMDb match status
            "tmdb_matched": bool(tmdb_data)
        }
        
        return formatted
    except Exception as e:
        return None

def format_series_for_xtream(show, category_id=2):
    """Format Plex TV show to Xtream Codes format - with TMDb posters"""
    try:
        # Get TMDb data for high-quality posters (with caching)
        tmdb_data = None
        if TMDB_API_KEY:
            cache_key = f"series_{show.ratingKey}"
            
            # Check cache first
            if cache_key in session_cache['series']:
                tmdb_data = session_cache['series'][cache_key]
            else:
                # Fetch and cache
                tmdb_data = enhance_series_with_tmdb(show)
                if tmdb_data:
                    session_cache['series'][cache_key] = tmdb_data
        
        # Use TMDb posters if available, otherwise Plex
        if tmdb_data and tmdb_data.get('poster_path'):
            poster_url = tmdb_data['poster_path']
        else:
            try:
                poster_url = show.thumbUrl if hasattr(show, 'thumbUrl') else ""
                if not poster_url and hasattr(show, 'thumb'):
                    poster_url = f"{PLEX_URL}{show.thumb}?X-Plex-Token={PLEX_TOKEN}"
            except:
                poster_url = ""
        
        # Use TMDb backdrop if available, otherwise Plex
        if tmdb_data and tmdb_data.get('backdrop_path'):
            backdrop_url = tmdb_data['backdrop_path']
        else:
            try:
                backdrop_url = show.artUrl if hasattr(show, 'artUrl') else ""
                if not backdrop_url and hasattr(show, 'art'):
                    backdrop_url = f"{PLEX_URL}{show.art}?X-Plex-Token={PLEX_TOKEN}"
            except:
                backdrop_url = ""
        
        # Full metadata with TMDb posters - multiple field names for compatibility
        formatted = {
            "series_id": show.ratingKey,
            "num": show.ratingKey,
            "name": show.title,
            # Multiple poster field names for different apps
            "cover": poster_url,
            "poster": poster_url,
            "image": poster_url,
            "icon": poster_url,
            # Backdrop fields
            "cover_big": backdrop_url,
            "backdrop": backdrop_url,
            "fanart": backdrop_url,
            "plot": show.summary if hasattr(show, 'summary') else "",
            "cast": ", ".join([actor.tag for actor in show.roles[:10]]) if hasattr(show, 'roles') and show.roles else "",
            "director": ", ".join([d.tag for d in show.directors]) if hasattr(show, 'directors') and show.directors else "",
            "genre": ", ".join([g.tag for g in show.genres]) if hasattr(show, 'genres') and show.genres else "",
            "releaseDate": str(show.year) if hasattr(show, 'year') and show.year else "",
            "year": str(show.year) if hasattr(show, 'year') and show.year else "",
            "rating": str(show.rating) if hasattr(show, 'rating') and show.rating else "0",
            "rating_5based": round(float(show.rating or 0) / 2, 1) if hasattr(show, 'rating') else 0,
            "backdrop_path": [backdrop_url] if backdrop_url else [],
            "category_id": str(category_id),
            # TMDb match status
            "tmdb_matched": bool(tmdb_data)
        }
        
        return formatted
    except Exception as e:
        return None
        print(f"Error formatting series {show.title}: {e}")
        import traceback
        traceback.print_exc()
        return None

def format_episode_for_xtream(episode, series_id):
    """Format Plex episode to Xtream Codes format"""
    try:
        stream_url = get_stream_url(episode)
        if not stream_url:
            return None
            
        return {
            "id": episode.ratingKey,
            "episode_num": episode.index,
            "title": episode.title,
            "container_extension": "mkv",
            "info": {
                "tmdb_id": "",
                "releasedate": episode.originallyAvailableAt.strftime("%Y-%m-%d") if episode.originallyAvailableAt else "",
                "plot": episode.summary or "",
                "duration_secs": str(episode.duration // 1000) if episode.duration else "0",
                "duration": str(episode.duration // 60000) if episode.duration else "0",
                "rating": str(episode.rating) if episode.rating else "0",
                "season": episode.seasonNumber,
                "cover_big": f"{PLEX_URL}{episode.thumb}?X-Plex-Token={PLEX_TOKEN}" if episode.thumb else ""
            },
            "direct_source": stream_url
        }
    except Exception as e:
        print(f"Error formatting episode {episode.title}: {e}")
        return None

# Web Interface Templates

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Plex Xtream Bridge - Dashboard</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        
        .header {
            background: white;
            padding: 30px;
            border-radius: 15px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            margin-bottom: 20px;
        }
        
        .header h1 {
            color: #333;
            margin-bottom: 10px;
        }
        
        .header p {
            color: #666;
        }
        
        .status-card {
            background: white;
            padding: 25px;
            border-radius: 15px;
            box-shadow: 0 5px 20px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }
        
        .status-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }
        
        .status-item {
            background: #f8f9fa;
            padding: 20px;
            border-radius: 10px;
            border-left: 4px solid #667eea;
        }
        
        .status-item h3 {
            color: #333;
            font-size: 14px;
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .status-item p {
            color: #666;
            font-size: 18px;
            font-weight: 600;
        }
        
        .status-badge {
            display: inline-block;
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 14px;
            font-weight: 600;
        }
        
        .status-connected {
            background: #d4edda;
            color: #155724;
        }
        
        .status-disconnected {
            background: #f8d7da;
            color: #721c24;
        }
        
        .button {
            display: inline-block;
            padding: 12px 24px;
            background: #667eea;
            color: white;
            text-decoration: none;
            border-radius: 8px;
            font-weight: 600;
            border: none;
            cursor: pointer;
            transition: background 0.3s;
        }
        
        .button:hover {
            background: #5568d3;
        }
        
        .button-secondary {
            background: #6c757d;
        }
        
        .button-secondary:hover {
            background: #5a6268;
        }
        
        .button-danger {
            background: #dc3545;
        }
        
        .button-danger:hover {
            background: #c82333;
        }
        
        .library-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 15px;
            margin-top: 20px;
        }
        
        .library-item {
            background: #f8f9fa;
            padding: 20px;
            border-radius: 10px;
            text-align: center;
        }
        
        .library-item h4 {
            color: #333;
            margin-bottom: 10px;
        }
        
        .library-count {
            font-size: 32px;
            font-weight: bold;
            color: #667eea;
        }
        
        .config-section {
            background: white;
            padding: 25px;
            border-radius: 15px;
            box-shadow: 0 5px 20px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }
        
        .config-section h2 {
            color: #333;
            margin-bottom: 20px;
        }
        
        .info-box {
            background: #e7f3ff;
            border-left: 4px solid #2196F3;
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 20px;
        }
        
        .code-box {
            background: #2d2d2d;
            color: #f8f8f2;
            padding: 15px;
            border-radius: 8px;
            font-family: 'Courier New', monospace;
            overflow-x: auto;
        }
        
        .action-buttons {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            margin-top: 20px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎬 Plex Xtream Bridge</h1>
            <p>Connect your Plex library to any Xtream UI player</p>
        </div>
        
        <div class="status-card">
            <h2 style="margin-bottom: 20px;">System Status</h2>
            <div class="status-grid">
                <div class="status-item">
                    <h3>Plex Server</h3>
                    <p>
                        {% if plex_connected %}
                        <span class="status-badge status-connected">✓ Connected</span>
                        {% else %}
                        <span class="status-badge status-disconnected">✗ Disconnected</span>
                        {% endif %}
                    </p>
                </div>
                
                <div class="status-item">
                    <h3>Server Name</h3>
                    <p>{{ server_name }}</p>
                </div>
                
                <div class="status-item">
                    <h3>Bridge URL</h3>
                    <p style="font-size: 14px;">{{ bridge_url }}</p>
                </div>
            </div>
            
            {% if plex_connected and libraries %}
            <h3 style="margin-top: 20px; margin-bottom: 10px;">Your Libraries</h3>
            <div class="library-grid">
                {% for lib in libraries %}
                <div class="library-item">
                    <h4>{{ lib.name }}</h4>
                    <div class="library-count">{{ lib.count }}</div>
                    <p style="color: #666; font-size: 12px; margin-top: 5px;">{{ lib.type }}</p>
                </div>
                {% endfor %}
            </div>
            {% endif %}
        </div>
        
        <div class="config-section">
            <h2>🚀 Quick Start Guide</h2>
            
            <div style="display: grid; gap: 15px;">
                <div style="background: #f8f9fa; padding: 20px; border-radius: 10px; border-left: 4px solid #667eea;">
                    <h3 style="margin-bottom: 10px; color: #333; font-size: 16px;">1️⃣ Configure Plex Connection</h3>
                    <p style="color: #666; margin-bottom: 10px;">Go to Settings and enter your Plex server URL and token.</p>
                    {% if not plex_connected %}
                    <a href="/admin/settings" class="button" style="display: inline-block; font-size: 14px; padding: 8px 16px;">Configure Now</a>
                    {% else %}
                    <span style="color: #28a745; font-weight: 600;">✓ Already configured</span>
                    {% endif %}
                </div>
                
                <div style="background: #f8f9fa; padding: 20px; border-radius: 10px; border-left: 4px solid #667eea;">
                    <h3 style="margin-bottom: 10px; color: #333; font-size: 16px;">2️⃣ Set Your Xtream Credentials</h3>
                    <p style="color: #666; margin-bottom: 10px;">Choose a username and password for your Xtream UI player.</p>
                    <p style="color: #666; margin-bottom: 10px;">Current: <code style="background: #fff; padding: 3px 6px; border-radius: 3px;">{{ bridge_username }}</code> / <code style="background: #fff; padding: 3px 6px; border-radius: 3px;">{{ bridge_password }}</code></p>
                    <a href="/admin/settings" class="button button-secondary" style="display: inline-block; font-size: 14px; padding: 8px 16px;">Change Credentials</a>
                </div>
                
                <div style="background: #f8f9fa; padding: 20px; border-radius: 10px; border-left: 4px solid #667eea;">
                    <h3 style="margin-bottom: 10px; color: #333; font-size: 16px;">3️⃣ Configure Your Player App</h3>
                    <p style="color: #666; margin-bottom: 10px;">Enter these details in your Xtream UI player (TiviMate, IPTV Smarters, etc.):</p>
                    <ul style="color: #666; margin-left: 20px;">
                        <li><strong>URL:</strong> {{ bridge_url }}</li>
                        <li><strong>Username:</strong> {{ bridge_username }}</li>
                        <li><strong>Password:</strong> {{ bridge_password }}</li>
                    </ul>
                </div>
            </div>
        </div>
        
        <div class="config-section">
            <h2>🔧 Configuration</h2>
            
            {% if not plex_connected %}
            <div class="info-box">
                <strong>⚠️ Not connected to Plex</strong><br>
                Please configure your Plex server connection below.
            </div>
            {% endif %}
            
            <div class="action-buttons">
                <a href="/admin/settings" class="button">⚙️ Settings</a>
                {% if tmdb_configured %}
                <a href="/admin/match-tmdb" class="button">🎬 Match Unmatched Movies/Series</a>
                {% endif %}
                <a href="/admin/test" class="button button-secondary">🧪 Test Connection</a>
                <a href="/admin/logout" class="button button-danger">🚪 Logout</a>
            </div>
        </div>
        
        <div class="config-section">
            <h2>📱 Xtream UI Player Configuration</h2>
            <div class="info-box">
                <strong>Use these credentials in your Xtream UI player</strong> (TiviMate, IPTV Smarters, GSE Smart IPTV, etc.)<br>
                You can change these anytime in Settings!
            </div>
            
            <div class="status-grid">
                <div class="status-item">
                    <h3>Server URL</h3>
                    <p style="font-size: 14px; word-break: break-all;">{{ bridge_url }}</p>
                </div>
                
                <div class="status-item">
                    <h3>Username</h3>
                    <p style="font-size: 16px; font-family: monospace; background: #f8f9fa; padding: 8px; border-radius: 5px;">{{ bridge_username }}</p>
                </div>
                
                <div class="status-item">
                    <h3>Password</h3>
                    <p style="font-size: 16px; font-family: monospace; background: #f8f9fa; padding: 8px; border-radius: 5px;">{{ bridge_password }}</p>
                </div>
            </div>
            
            <h3 style="margin-top: 20px; margin-bottom: 10px;">Test API Endpoint</h3>
            <div class="code-box">
{{ bridge_url }}/player_api.php?username={{ bridge_username }}&password={{ bridge_password }}
            </div>
            
            <div style="margin-top: 15px; padding: 12px; background: #e7f3ff; border-left: 4px solid #2196F3; border-radius: 5px;">
                <small><strong>💡 Want to change these?</strong> Go to Settings → Xtream UI Player Credentials</small>
            </div>
        </div>
    </div>
</body>
</html>
"""

SETTINGS_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Settings - Plex Xtream Bridge</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        
        .container {
            max-width: 800px;
            margin: 0 auto;
        }
        
        .card {
            background: white;
            padding: 30px;
            border-radius: 15px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            margin-bottom: 20px;
        }
        
        .card h1 {
            color: #333;
            margin-bottom: 10px;
        }
        
        .card h2 {
            color: #333;
            margin-bottom: 20px;
            font-size: 20px;
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        .form-group label {
            display: block;
            color: #333;
            font-weight: 600;
            margin-bottom: 8px;
        }
        
        .form-group input {
            width: 100%;
            padding: 12px;
            border: 2px solid #e1e4e8;
            border-radius: 8px;
            font-size: 14px;
            transition: border-color 0.3s;
        }
        
        .form-group input:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .form-group small {
            display: block;
            color: #666;
            margin-top: 5px;
            font-size: 12px;
        }
        
        .button {
            display: inline-block;
            padding: 12px 24px;
            background: #667eea;
            color: white;
            text-decoration: none;
            border-radius: 8px;
            font-weight: 600;
            border: none;
            cursor: pointer;
            transition: background 0.3s;
        }
        
        .button:hover {
            background: #5568d3;
        }
        
        .button-secondary {
            background: #6c757d;
        }
        
        .button-secondary:hover {
            background: #5a6268;
        }
        
        .button-group {
            display: flex;
            gap: 10px;
            margin-top: 20px;
        }
        
        .alert {
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
        }
        
        .alert-success {
            background: #d4edda;
            color: #155724;
            border-left: 4px solid #28a745;
        }
        
        .alert-error {
            background: #f8d7da;
            color: #721c24;
            border-left: 4px solid #dc3545;
        }
        
        .alert-info {
            background: #d1ecf1;
            color: #0c5460;
            border-left: 4px solid #17a2b8;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h1>⚙️ Settings</h1>
            <p style="color: #666; margin-bottom: 20px;">Configure your Plex connection and bridge credentials</p>
            
            <div style="background: #d4edda; border-left: 4px solid #28a745; padding: 12px; border-radius: 5px; margin-bottom: 20px; color: #155724;">
                <strong>🔒 Security:</strong> Your Plex token and TMDb API key are encrypted before being saved. Passwords are hashed using SHA-256.
            </div>
            
            <div class="action-buttons" style="display: flex; gap: 10px; margin-bottom: 20px;">
                <a href="/admin" class="button button-secondary">← Back to Dashboard</a>
                <button onclick="clearCache()" class="button button-secondary">🔄 Clear Cache</button>
            </div>
        </div>
        
        {% if message %}
        <div class="alert {% if error %}alert-error{% else %}alert-success{% endif %}">
            {{ message }}
        </div>
        {% endif %}
        
        <form method="POST" action="/admin/settings">
            <div class="card">
                <h2>Plex Server Configuration</h2>
                
                <div class="form-group">
                    <label for="plex_url">Plex Server URL</label>
                    <input type="text" id="plex_url" name="plex_url" value="{{ plex_url }}" placeholder="http://192.168.1.100:32400" required>
                    <small>The URL of your Plex Media Server (include http:// or https://)</small>
                </div>
                
                <div class="form-group">
                    <label for="plex_token">Plex Token</label>
                    <input type="text" id="plex_token" name="plex_token" value="{{ plex_token }}" placeholder="Your Plex authentication token" required>
                    <small>Get from Plex Web: Play media → Get Info → View XML → Copy X-Plex-Token</small>
                </div>
                
                <div class="form-group">
                    <label for="tmdb_api_key">TMDb API Key (Optional)</label>
                    <input type="text" id="tmdb_api_key" name="tmdb_api_key" value="{{ tmdb_api_key }}" placeholder="Your TMDb API key for enhanced metadata">
                    <small>Get free API key from <a href="https://www.themoviedb.org/settings/api" target="_blank">themoviedb.org/settings/api</a> - enables better categorization</small>
                </div>
            </div>
            
            <div class="card">
                <h2>Xtream UI Player Credentials</h2>
                
                <div class="alert alert-info">
                    <strong>⚠️ Important:</strong> These are the credentials you'll use in your Xtream UI player (TiviMate, IPTV Smarters, etc.) to connect to this bridge. You can change them to anything you want!
                </div>
                
                <div class="form-group">
                    <label for="bridge_username">Xtream Username</label>
                    <input type="text" id="bridge_username" name="bridge_username" value="{{ bridge_username }}" required placeholder="myusername">
                    <small>This is the username you'll enter in your Xtream UI player</small>
                </div>
                
                <div class="form-group">
                    <label for="bridge_password">Xtream Password</label>
                    <input type="text" id="bridge_password" name="bridge_password" value="{{ bridge_password }}" required placeholder="mysecurepassword">
                    <small>This is the password you'll enter in your Xtream UI player</small>
                </div>
                
                <div style="background: #fff3cd; border-left: 4px solid #ffc107; padding: 12px; border-radius: 5px; margin-top: 15px;">
                    <small><strong>💡 Tip:</strong> After changing these, you'll need to update them in your Xtream UI player app as well!</small>
                </div>
            </div>
            
            <div class="card">
                <h2>Admin Panel Security</h2>
                
                <div class="form-group">
                    <label for="admin_password">Admin Panel Password</label>
                    <input type="password" id="admin_password" name="admin_password" value="{{ admin_password }}" required>
                    <small>Password to access this admin panel</small>
                </div>
            </div>
            
            <div class="card">
                <h2>Advanced Options</h2>
                
                <div class="form-group">
                    <label style="display: flex; align-items: center; cursor: pointer;">
                        <input type="checkbox" name="show_dummy_channel" {% if show_dummy_channel %}checked{% endif %} style="width: auto; margin-right: 10px;">
                        <span>Show Info Channel in Live TV</span>
                    </label>
                    <small>Display a dummy "Plex Bridge Info" channel in the Live TV section. Useful to prevent empty Live TV categories in players. Disable this if you have real Plex Live TV/DVR.</small>
                </div>
            </div>
            
            <div class="card">
                <div class="button-group">
                    <button type="submit" class="button">💾 Save Settings</button>
                    <a href="/admin" class="button button-secondary">Cancel</a>
                </div>
            </div>
        </form>
    </div>
</body>
</html>
"""

LOGIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - Plex Xtream Bridge</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        
        .login-card {
            background: white;
            padding: 40px;
            border-radius: 15px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            max-width: 400px;
            width: 100%;
        }
        
        .login-card h1 {
            color: #333;
            margin-bottom: 10px;
            text-align: center;
        }
        
        .login-card p {
            color: #666;
            text-align: center;
            margin-bottom: 30px;
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        .form-group label {
            display: block;
            color: #333;
            font-weight: 600;
            margin-bottom: 8px;
        }
        
        .form-group input {
            width: 100%;
            padding: 12px;
            border: 2px solid #e1e4e8;
            border-radius: 8px;
            font-size: 14px;
        }
        
        .form-group input:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .button {
            width: 100%;
            padding: 12px;
            background: #667eea;
            color: white;
            border: none;
            border-radius: 8px;
            font-weight: 600;
            cursor: pointer;
            font-size: 16px;
        }
        
        .button:hover {
            background: #5568d3;
        }
        
        .alert-error {
            background: #f8d7da;
            color: #721c24;
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 20px;
            border-left: 4px solid #dc3545;
        }
    </style>
</head>
<body>
    <div class="login-card">
        <h1>🔐 Admin Login</h1>
        <p>Plex Xtream Bridge</p>
        
        {% if error %}
        <div class="alert-error">
            {{ error }}
        </div>
        {% endif %}
        
        <form method="POST" action="/admin/login">
            <div class="form-group">
                <label for="password">Password</label>
                <input type="password" id="password" name="password" required autofocus>
            </div>
            
            <button type="submit" class="button">Login</button>
        </form>
    </div>
</body>
</html>
"""

CHANGE_PASSWORD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>First Time Setup - Plex Xtream Bridge</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        
        .setup-card {
            background: white;
            padding: 40px;
            border-radius: 15px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            max-width: 550px;
            width: 100%;
        }
        
        .setup-card h1 {
            color: #333;
            margin-bottom: 10px;
            text-align: center;
        }
        
        .setup-card p {
            color: #666;
            text-align: center;
            margin-bottom: 30px;
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        .form-group label {
            display: block;
            color: #333;
            font-weight: 600;
            margin-bottom: 8px;
        }
        
        .form-group input {
            width: 100%;
            padding: 12px;
            border: 2px solid #e1e4e8;
            border-radius: 8px;
            font-size: 14px;
        }
        
        .form-group input:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .form-group small {
            display: block;
            color: #666;
            margin-top: 5px;
            font-size: 12px;
        }
        
        .button {
            width: 100%;
            padding: 12px;
            background: #667eea;
            color: white;
            border: none;
            border-radius: 8px;
            font-weight: 600;
            cursor: pointer;
            font-size: 16px;
        }
        
        .button:hover {
            background: #5568d3;
        }
        
        .alert-warning {
            background: #fff3cd;
            color: #856404;
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
            border-left: 4px solid #ffc107;
        }
        
        .alert-error {
            background: #f8d7da;
            color: #721c24;
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 20px;
            border-left: 4px solid #dc3545;
        }
        
        .section-divider {
            border-top: 2px solid #e1e4e8;
            margin: 30px 0;
            position: relative;
        }
        
        .section-divider span {
            background: white;
            padding: 0 15px;
            position: absolute;
            top: -12px;
            left: 50%;
            transform: translateX(-50%);
            color: #666;
            font-weight: 600;
            font-size: 14px;
        }
        
        .info-box {
            background: #e7f3ff;
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
            border-left: 4px solid #2196F3;
        }
        
        .info-box h3 {
            color: #333;
            font-size: 14px;
            margin-bottom: 10px;
        }
        
        .info-box ul {
            margin-left: 20px;
            color: #666;
            font-size: 13px;
            line-height: 1.6;
        }
    </style>
</head>
<body>
    <div class="setup-card">
        <h1>🔒 First Time Setup</h1>
        <p>Secure your bridge with strong credentials</p>
        
        <div class="alert-warning">
            <strong>⚠️ Security Required</strong><br>
            You're using default credentials. Please set secure passwords for both admin panel and Xtream players.
        </div>
        
        {% if error %}
        <div class="alert-error">
            {{ error }}
        </div>
        {% endif %}
        
        <form method="POST" action="/admin/change-password">
            <h2 style="color: #333; font-size: 18px; margin-bottom: 15px;">1️⃣ Admin Panel Password</h2>
            <div class="info-box">
                <p style="font-size: 13px; color: #666;">This password protects the web interface at /admin</p>
            </div>
            
            <div class="form-group">
                <label for="new_password">Admin Password</label>
                <input type="password" id="new_password" name="new_password" required minlength="8" autofocus>
                <small>Minimum 8 characters - protects web interface</small>
            </div>
            
            <div class="form-group">
                <label for="confirm_password">Confirm Admin Password</label>
                <input type="password" id="confirm_password" name="confirm_password" required minlength="8">
                <small>Re-enter your admin password</small>
            </div>
            
            <div class="section-divider">
                <span>AND</span>
            </div>
            
            <h2 style="color: #333; font-size: 18px; margin-bottom: 15px;">2️⃣ Xtream Player Credentials</h2>
            <div class="info-box">
                <p style="font-size: 13px; color: #666;">These credentials are used by your Xtream UI player (TiviMate, IPTV Smarters, etc.)</p>
            </div>
            
            <div class="form-group">
                <label for="bridge_username">Player Username</label>
                <input type="text" id="bridge_username" name="bridge_username" required minlength="3" placeholder="myusername">
                <small>Username for your Xtream player - can be anything</small>
            </div>
            
            <div class="form-group">
                <label for="bridge_password">Player Password</label>
                <input type="text" id="bridge_password" name="bridge_password" required minlength="8" placeholder="mysecurepassword">
                <small>Password for your Xtream player - minimum 8 characters</small>
            </div>
            
            <button type="submit" class="button">🔒 Secure My Bridge & Continue</button>
        </form>
        
        <div class="info-box" style="margin-top: 20px;">
            <h3>💡 Password Tips:</h3>
            <ul>
                <li>Use at least 8 characters for both passwords</li>
                <li>Mix uppercase, lowercase, numbers, symbols</li>
                <li>Don't reuse passwords from other sites</li>
                <li>Different passwords for admin vs player is OK</li>
                <li>Consider using a password manager</li>
            </ul>
        </div>
    </div>
</body>
</html>
"""

# Decorator for admin routes
def require_admin_login(f):
    """Decorator to require admin login"""
    def decorated_function(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    decorated_function.__name__ = f.__name__
    return decorated_function

# Web Interface Routes

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    """Admin login page"""
    global ADMIN_PASSWORD
    
    if request.method == 'POST':
        password = request.form.get('password')
        
        # Check against current password (could be plain text or hashed)
        password_match = False
        
        # Check plain text (for initial setup or legacy)
        if password == ADMIN_PASSWORD:
            password_match = True
        # Check hashed password
        elif hash_password(password) == ADMIN_PASSWORD:
            password_match = True
        
        if password_match:
            # Check if this is first login with default password
            if password == 'admin123':
                session['needs_password_change'] = True
                session['temp_authenticated'] = True
                return redirect(url_for('change_password'))
            
            session['admin_logged_in'] = True
            session.pop('needs_password_change', None)
            session.pop('temp_authenticated', None)
            return redirect(url_for('admin_dashboard'))
        else:
            return render_template_string(LOGIN_HTML, error="Invalid password")
    
    return render_template_string(LOGIN_HTML)

@app.route('/admin/change-password', methods=['GET', 'POST'])
def change_password():
    """Force password change and Xtream credentials setup on first login"""
    global ADMIN_PASSWORD, BRIDGE_USERNAME, BRIDGE_PASSWORD
    
    # Must be in temp authenticated state (logged in with default password)
    if not session.get('temp_authenticated') and not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    
    if request.method == 'POST':
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        bridge_username = request.form.get('bridge_username')
        bridge_password = request.form.get('bridge_password')
        
        # Validation - Admin Password
        if not new_password or len(new_password) < 8:
            return render_template_string(CHANGE_PASSWORD_HTML, 
                error="Admin password must be at least 8 characters long")
        
        if new_password != confirm_password:
            return render_template_string(CHANGE_PASSWORD_HTML,
                error="Admin passwords do not match")
        
        if new_password == 'admin123':
            return render_template_string(CHANGE_PASSWORD_HTML,
                error="Cannot use default password 'admin123'. Please choose a different password.")
        
        # Validation - Xtream Credentials
        if not bridge_username or len(bridge_username) < 3:
            return render_template_string(CHANGE_PASSWORD_HTML,
                error="Player username must be at least 3 characters long")
        
        if not bridge_password or len(bridge_password) < 8:
            return render_template_string(CHANGE_PASSWORD_HTML,
                error="Player password must be at least 8 characters long")
        
        if bridge_username == 'admin' and bridge_password == 'admin':
            return render_template_string(CHANGE_PASSWORD_HTML,
                error="Cannot use default Xtream credentials 'admin/admin'. Please choose different values.")
        
        # Update all credentials
        ADMIN_PASSWORD = new_password
        BRIDGE_USERNAME = bridge_username
        BRIDGE_PASSWORD = bridge_password
        
        # Save config (will be hashed/encrypted in save_config)
        if save_config():
            # Mark as fully authenticated
            session['admin_logged_in'] = True
            session.pop('needs_password_change', None)
            session.pop('temp_authenticated', None)
            
            return redirect(url_for('admin_dashboard'))
        else:
            return render_template_string(CHANGE_PASSWORD_HTML,
                error="Failed to save new credentials")
    
    return render_template_string(CHANGE_PASSWORD_HTML)

@app.route('/admin/create-category', methods=['POST'])
@require_admin_login
def create_custom_category():
    """Create a new custom category with filter code"""
    try:
        category_name = request.form.get('category_name')
        category_type = request.form.get('category_type', 'movies')
        filter_code = request.form.get('filter_code')
        max_items = int(request.form.get('max_items', 100))
        
        # Generate unique ID
        category_id = str(30000 + len(custom_categories.get(category_type, [])))
        
        # Create category
        new_category = {
            'id': category_id,
            'name': f"🎯 {category_name}",
            'type': 'custom_filter',
            'filter_code': filter_code,
            'limit': max_items
        }
        
        # Add to appropriate list
        if category_type not in custom_categories:
            custom_categories[category_type] = []
        
        custom_categories[category_type].append(new_category)
        
        # Save categories
        save_categories()
        
        # Clear cache to reload with new category
        clear_metadata_cache()
        
        return redirect(url_for('admin_categories'))
    except Exception as e:
        print(f"Error creating category: {e}")
        return redirect(url_for('category_editor'))

@app.route('/admin/logout')
def admin_logout():
    """Logout"""
    session.pop('admin_logged_in', None)
    return redirect('/admin/login')

@app.route('/admin/match-tmdb')
@require_admin_login
def tmdb_matcher():
    """TMDb manual matching page"""
    if not TMDB_API_KEY:
        return redirect('/admin/settings')
    
    # Get page numbers from query params
    movie_page = int(request.args.get('movie_page', 1))
    show_page = int(request.args.get('show_page', 1))
    per_page = 20
    
    # Get unmatched content
    unmatched_movies = []
    unmatched_shows = []
    
    if plex:
        # Check movies
        for section in plex.library.sections():
            if section.type == 'movie':
                for movie in section.all():
                    cache_key = f"movie_{movie.ratingKey}"
                    if cache_key not in session_cache['movies']:
                        unmatched_movies.append({
                            'id': movie.ratingKey,
                            'title': movie.title,
                            'year': movie.year if hasattr(movie, 'year') else ''
                        })
            elif section.type == 'show':
                for show in section.all():
                    cache_key = f"series_{show.ratingKey}"
                    if cache_key not in session_cache['series']:
                        unmatched_shows.append({
                            'id': show.ratingKey,
                            'title': show.title,
                            'year': show.year if hasattr(show, 'year') else ''
                        })
    
    # Pagination calculations
    total_movies = len(unmatched_movies)
    total_shows = len(unmatched_shows)
    
    movie_total_pages = (total_movies + per_page - 1) // per_page if total_movies > 0 else 1
    show_total_pages = (total_shows + per_page - 1) // per_page if total_shows > 0 else 1
    
    movie_start = (movie_page - 1) * per_page
    movie_end = movie_start + per_page
    
    show_start = (show_page - 1) * per_page
    show_end = show_start + per_page
    
    movies_paginated = unmatched_movies[movie_start:movie_end]
    shows_paginated = unmatched_shows[show_start:show_end]
    
    # Generate pagination HTML
    def generate_pagination(current_page, total_pages, content_type):
        if total_pages <= 1:
            return ""
        
        parts = []
        parts.append('<div class="pagination">')
        
        # Previous button
        if current_page > 1:
            prev_movie_page = movie_page if content_type == "show" else current_page - 1
            prev_show_page = show_page if content_type == "movie" else current_page - 1
            parts.append(f'<a href="?movie_page={prev_movie_page}&show_page={prev_show_page}" class="page-btn">← Previous</a>')
        else:
            parts.append('<span class="page-btn disabled">← Previous</span>')
        
        # Page numbers
        for page in range(1, total_pages + 1):
            if page == current_page:
                parts.append(f'<span class="page-btn active">{page}</span>')
            elif abs(page - current_page) <= 2 or page == 1 or page == total_pages:
                page_movie_page = movie_page if content_type == "show" else page
                page_show_page = show_page if content_type == "movie" else page
                parts.append(f'<a href="?movie_page={page_movie_page}&show_page={page_show_page}" class="page-btn">{page}</a>')
            elif abs(page - current_page) == 3:
                parts.append('<span class="page-btn disabled">...</span>')
        
        # Next button
        if current_page < total_pages:
            next_movie_page = movie_page if content_type == "show" else current_page + 1
            next_show_page = show_page if content_type == "movie" else current_page + 1
            parts.append(f'<a href="?movie_page={next_movie_page}&show_page={next_show_page}" class="page-btn">Next →</a>')
        else:
            parts.append('<span class="page-btn disabled">Next →</span>')
        
        parts.append('</div>')
        return ''.join(parts)
    
    movie_pagination = generate_pagination(movie_page, movie_total_pages, 'movie')
    show_pagination = generate_pagination(show_page, show_total_pages, 'show')
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Match Unmatched Content - Plex Xtream Bridge</title>
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; padding: 20px; }}
            .container {{ max-width: 1400px; margin: 0 auto; background: white; border-radius: 20px; padding: 40px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); }}
            h1 {{ color: #333; margin-bottom: 10px; }}
            .subtitle {{ color: #666; margin-bottom: 30px; }}
            .button {{ display: inline-block; background: #667eea; color: white; padding: 12px 24px; text-decoration: none; border-radius: 8px; transition: all 0.3s; border: none; cursor: pointer; font-size: 14px; }}
            .button:hover {{ background: #5568d3; transform: translateY(-2px); }}
            .button-secondary {{ background: #6c757d; }}
            .button-secondary:hover {{ background: #5a6268; }}
            .button-small {{ padding: 8px 16px; font-size: 12px; }}
            .button-success {{ background: #28a745; }}
            .button-success:hover {{ background: #218838; }}
            .unmatched-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(400px, 1fr)); gap: 20px; margin-top: 20px; }}
            .unmatched-item {{ background: #f8f9fa; padding: 20px; border-radius: 8px; border-left: 4px solid #667eea; }}
            .unmatched-item h3 {{ color: #333; margin-bottom: 5px; font-size: 16px; }}
            .unmatched-item .year {{ color: #666; font-size: 14px; margin-bottom: 15px; }}
            .search-box {{ width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; margin-bottom: 10px; }}
            .search-results {{ max-height: 300px; overflow-y: auto; background: white; border: 1px solid #ddd; border-radius: 4px; margin-top: 10px; display: none; }}
            .search-result {{ padding: 10px; border-bottom: 1px solid #eee; cursor: pointer; transition: background 0.2s; }}
            .search-result:hover {{ background: #f0f0f0; }}
            .search-result-title {{ font-weight: bold; color: #333; }}
            .search-result-year {{ color: #666; font-size: 12px; }}
            .search-result-overview {{ color: #999; font-size: 11px; margin-top: 5px; }}
            .section-header {{ margin-top: 30px; margin-bottom: 15px; color: #333; border-bottom: 2px solid #667eea; padding-bottom: 10px; display: flex; justify-content: space-between; align-items: center; }}
            .info-box {{ background: #e7f3ff; border-left: 4px solid #2196F3; padding: 15px; margin-bottom: 20px; border-radius: 4px; }}
            .matched {{ opacity: 0.5; pointer-events: none; }}
            .matched-badge {{ background: #28a745; color: white; padding: 4px 8px; border-radius: 4px; font-size: 12px; display: inline-block; margin-top: 10px; }}
            .pagination {{ display: flex; gap: 5px; justify-content: center; align-items: center; margin: 20px 0; flex-wrap: wrap; }}
            .page-btn {{ padding: 8px 12px; background: #667eea; color: white; text-decoration: none; border-radius: 4px; transition: all 0.2s; font-size: 14px; }}
            .page-btn:hover {{ background: #5568d3; }}
            .page-btn.active {{ background: #764ba2; font-weight: bold; }}
            .page-btn.disabled {{ background: #ccc; cursor: not-allowed; pointer-events: none; }}
            .count-badge {{ background: #667eea; color: white; padding: 4px 12px; border-radius: 12px; font-size: 14px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🎬 Match Unmatched Content</h1>
            <p class="subtitle">Manually match content with TMDb</p>
            
            <div class="info-box">
                <strong>ℹ️ How to use:</strong> Type in the search box to find the correct TMDb match for each item. Click on a result to match it.
            </div>
            
            <div style="display: flex; gap: 10px; margin-bottom: 20px;">
                <a href="/admin" class="button button-secondary">← Back to Dashboard</a>
                <button onclick="triggerAutoMatch()" id="autoMatchBtn" class="button">🔄 Auto-Match Unmatched</button>
            </div>
            
            <div id="autoMatchStatus" style="display: none; padding: 15px; background: #e7f3ff; border-left: 4px solid #2196F3; border-radius: 4px; margin-bottom: 20px;">
                <strong>⏳ Auto-matching in progress...</strong>
                <p style="margin: 5px 0 0 0;">This may take a few minutes. The page will refresh when complete.</p>
            </div>
            
            <div style="margin: 30px 0; padding: 20px; background: #f8f9fa; border-radius: 8px;">
                <h3 style="margin-bottom: 15px; color: #333;">🔍 Search & Fix Any Content</h3>
                <input type="text" id="globalSearch" class="search-box" placeholder="Search for any movie or TV show to fix its TMDb match..." 
                       onkeyup="globalSearch(this.value)" style="margin-bottom: 10px;">
                <div style="display: flex; gap: 10px; margin-bottom: 10px;">
                    <label style="display: flex; align-items: center; gap: 5px;">
                        <input type="radio" name="searchType" value="movie" checked onchange="clearGlobalResults()"> Movies
                    </label>
                    <label style="display: flex; align-items: center; gap: 5px;">
                        <input type="radio" name="searchType" value="show" onchange="clearGlobalResults()"> TV Shows
                    </label>
                </div>
                <div id="globalSearchResults" style="max-height: 400px; overflow-y: auto;"></div>
            </div>
            
            <h2 class="section-header">
                <span>📽️ Unmatched Movies</span>
                <span class="count-badge">{total_movies} total • Page {movie_page} of {movie_total_pages}</span>
            </h2>
            
            {movie_pagination}
            
            <div class="unmatched-grid">
                {''.join([f'''
                <div class="unmatched-item" id="movie-{m["id"]}">
                    <h3>{m["title"]}</h3>
                    <p class="year">{m["year"]}</p>
                    <input type="text" class="search-box" placeholder="Search TMDb for '{m["title"]}'..." 
                           onkeyup="searchTMDb('{m["id"]}', 'movie', this.value)">
                    <div class="search-results" id="results-movie-{m["id"]}"></div>
                </div>
                ''' for m in movies_paginated])}
            </div>
            
            {movie_pagination}
            
            <h2 class="section-header">
                <span>📺 Unmatched TV Shows</span>
                <span class="count-badge">{total_shows} total • Page {show_page} of {show_total_pages}</span>
            </h2>
            
            {show_pagination}
            
            <div class="unmatched-grid">
                {''.join([f'''
                <div class="unmatched-item" id="show-{s["id"]}">
                    <h3>{s["title"]}</h3>
                    <p class="year">{s["year"]}</p>
                    <input type="text" class="search-box" placeholder="Search TMDb for '{s["title"]}'..." 
                           onkeyup="searchTMDb('{s["id"]}', 'show', this.value)">
                    <div class="search-results" id="results-show-{s["id"]}"></div>
                </div>
                ''' for s in shows_paginated])}
            </div>
            
            {show_pagination}
        </div>
        
        <script>
        let searchTimeout = null;
        let globalSearchTimeout = null;
        
        function triggerAutoMatch() {{
            const btn = document.getElementById('autoMatchBtn');
            const status = document.getElementById('autoMatchStatus');
            
            btn.disabled = true;
            btn.textContent = '⏳ Auto-matching...';
            status.style.display = 'block';
            
            fetch('/admin/trigger-auto-match', {{ method: 'POST' }})
                .then(r => r.json())
                .then(data => {{
                    if (data.success) {{
                        setTimeout(() => {{
                            location.reload();
                        }}, 2000);
                    }} else {{
                        alert('Auto-match failed: ' + (data.error || 'Unknown error'));
                        btn.disabled = false;
                        btn.textContent = '🔄 Auto-Match Unmatched';
                        status.style.display = 'none';
                    }}
                }})
                .catch(err => {{
                    alert('Error: ' + err);
                    btn.disabled = false;
                    btn.textContent = '🔄 Auto-Match Unmatched';
                    status.style.display = 'none';
                }});
        }}
        
        function clearGlobalResults() {{
            document.getElementById('globalSearchResults').innerHTML = '';
        }}
        
        function globalSearch(query) {{
            clearTimeout(globalSearchTimeout);
            
            if (query.length < 2) {{
                document.getElementById('globalSearchResults').innerHTML = '';
                return;
            }}
            
            const searchType = document.querySelector('input[name="searchType"]:checked').value;
            
            globalSearchTimeout = setTimeout(() => {{
                fetch('/admin/search-plex?query=' + encodeURIComponent(query) + '&type=' + searchType)
                    .then(r => r.json())
                    .then(data => {{
                        const resultsDiv = document.getElementById('globalSearchResults');
                        if (data.results && data.results.length > 0) {{
                            resultsDiv.innerHTML = data.results.map(item => `
                                <div style="background: white; padding: 15px; margin-bottom: 10px; border-radius: 8px; border-left: 4px solid #667eea;">
                                    <div style="display: flex; justify-content: space-between; align-items: start;">
                                        <div>
                                            <h4 style="margin: 0 0 5px 0; color: #333;">${{item.title}}</h4>
                                            <p style="margin: 0; color: #666; font-size: 14px;">${{item.year || 'N/A'}}</p>
                                            <p style="margin: 5px 0 0 0; color: #999; font-size: 12px;">
                                                ${{item.matched ? '✓ Already matched to TMDb' : '⚠️ Not matched'}}
                                            </p>
                                        </div>
                                        <button onclick="showFixDialog('${{item.id}}', '${{searchType}}', '${{item.title.replace(/'/g, "\\\\'")}}')" 
                                                class="button button-small" style="margin-left: 10px;">
                                            ${{item.matched ? 'Re-match' : 'Match'}}
                                        </button>
                                    </div>
                                    <div id="fix-dialog-${{item.id}}" style="display: none; margin-top: 15px; padding-top: 15px; border-top: 1px solid #eee;">
                                        <input type="text" class="search-box" placeholder="Search TMDb for '${{item.title}}'..." 
                                               onkeyup="searchTMDbForFix('${{item.id}}', '${{searchType}}', this.value)">
                                        <div id="fix-results-${{item.id}}" class="search-results"></div>
                                    </div>
                                </div>
                            `).join('');
                        }} else {{
                            resultsDiv.innerHTML = '<div style="padding: 10px; color: #999;">No results found in your Plex library</div>';
                        }}
                    }});
            }}, 500);
        }}
        
        function showFixDialog(plexId, type, title) {{
            const dialog = document.getElementById('fix-dialog-' + plexId);
            dialog.style.display = dialog.style.display === 'none' ? 'block' : 'none';
        }}
        
        function searchTMDbForFix(plexId, type, query) {{
            clearTimeout(searchTimeout);
            
            if (query.length < 2) {{
                document.getElementById('fix-results-' + plexId).style.display = 'none';
                return;
            }}
            
            searchTimeout = setTimeout(() => {{
                fetch('/admin/search-tmdb?query=' + encodeURIComponent(query) + '&type=' + type)
                    .then(r => r.json())
                    .then(data => {{
                        const resultsDiv = document.getElementById('fix-results-' + plexId);
                        if (data.results && data.results.length > 0) {{
                            resultsDiv.innerHTML = data.results.map(r => `
                                <div class="search-result" onclick="matchContent('${{plexId}}', '${{type}}', ${{r.id}}, '${{r.title || r.name}}', true)">
                                    <div class="search-result-title">${{r.title || r.name}}</div>
                                    <div class="search-result-year">${{r.release_date || r.first_air_date || 'N/A'}}</div>
                                    <div class="search-result-overview">${{(r.overview || '').substring(0, 100)}}...</div>
                                </div>
                            `).join('');
                            resultsDiv.style.display = 'block';
                        }} else {{
                            resultsDiv.innerHTML = '<div style="padding: 10px; color: #999;">No results found</div>';
                            resultsDiv.style.display = 'block';
                        }}
                    }});
            }}, 500);
        }}
        
        function searchTMDb(plexId, type, query) {{
            clearTimeout(searchTimeout);
            
            if (query.length < 2) {{
                document.getElementById('results-' + type + '-' + plexId).style.display = 'none';
                return;
            }}
            
            searchTimeout = setTimeout(() => {{
                fetch('/admin/search-tmdb?query=' + encodeURIComponent(query) + '&type=' + type)
                    .then(r => r.json())
                    .then(data => {{
                        const resultsDiv = document.getElementById('results-' + type + '-' + plexId);
                        if (data.results && data.results.length > 0) {{
                            resultsDiv.innerHTML = data.results.map(r => `
                                <div class="search-result" onclick="matchContent('${{plexId}}', '${{type}}', ${{r.id}}, '${{r.title || r.name}}', false)">
                                    <div class="search-result-title">${{r.title || r.name}}</div>
                                    <div class="search-result-year">${{r.release_date || r.first_air_date || 'N/A'}}</div>
                                    <div class="search-result-overview">${{(r.overview || '').substring(0, 100)}}...</div>
                                </div>
                            `).join('');
                            resultsDiv.style.display = 'block';
                        }} else {{
                            resultsDiv.innerHTML = '<div style="padding: 10px; color: #999;">No results found</div>';
                            resultsDiv.style.display = 'block';
                        }}
                    }});
            }}, 500);
        }}
        
        function matchContent(plexId, type, tmdbId, title, isGlobalSearch) {{
            fetch('/admin/match-content', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ plex_id: plexId, type: type, tmdb_id: tmdbId }})
            }})
            .then(r => r.json())
            .then(data => {{
                if (data.success) {{
                    if (isGlobalSearch) {{
                        alert('✓ Successfully matched to: ' + title);
                        document.getElementById('fix-dialog-' + plexId).innerHTML = '<div class="matched-badge">✓ Matched to: ' + title + '</div>';
                    }} else {{
                        const item = document.getElementById(type + '-' + plexId);
                        item.classList.add('matched');
                        item.innerHTML += '<div class="matched-badge">✓ Matched to: ' + title + '</div>';
                        setTimeout(() => {{ 
                            item.style.display = 'none';
                            // Reload page if all items on current page are matched
                            const remainingItems = document.querySelectorAll('.unmatched-item:not(.matched)').length;
                            if (remainingItems === 0) {{
                                location.reload();
                            }}
                        }}, 2000);
                    }}
                }} else {{
                    alert('Failed to match: ' + (data.error || 'Unknown error'));
                }}
            }});
        }}
        </script>
    </body>
    </html>
    """
    return html

@app.route('/admin/search-tmdb')
@require_admin_login
def search_tmdb_api():
    """Search TMDb API"""
    query = request.args.get('query', '')
    content_type = request.args.get('type', 'movie')
    
    if not TMDB_API_KEY or not query:
        return jsonify({"results": []})
    
    try:
        tmdb_type = 'tv' if content_type == 'show' else 'movie'
        url = f"https://api.themoviedb.org/3/search/{tmdb_type}"
        params = {
            'api_key': TMDB_API_KEY,
            'query': query,
            'include_adult': 'false'
        }
        response = requests.get(url, params=params, timeout=5)
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"results": []})
    except Exception as e:
        print(f"[ERROR] TMDb search error: {e}")
        return jsonify({"results": []})

@app.route('/admin/trigger-auto-match', methods=['POST'])
@require_admin_login
def trigger_auto_match():
    """Manually trigger auto-matching"""
    if auto_matching_running:
        return jsonify({"success": False, "error": "Auto-matching already in progress"})
    
    # Run auto-match in background thread
    threading.Thread(target=auto_match_content, daemon=True).start()
    
    return jsonify({"success": True})

@app.route('/admin/search-plex')
@require_admin_login
def search_plex_api():
    """Search Plex library"""
    query = request.args.get('query', '').lower()
    content_type = request.args.get('type', 'movie')
    
    if not query or not plex:
        return jsonify({"results": []})
    
    results = []
    
    try:
        for section in plex.library.sections():
            plex_type = 'movie' if content_type == 'movie' else 'show'
            if section.type == plex_type:
                # Search in this section
                for item in section.all():
                    if query in item.title.lower():
                        # Check if already matched
                        cache_key = f"{content_type}_{item.ratingKey}" if content_type == 'movie' else f"series_{item.ratingKey}"
                        cache_category = 'movies' if content_type == 'movie' else 'series'
                        matched = cache_key in session_cache[cache_category]
                        
                        results.append({
                            'id': item.ratingKey,
                            'title': item.title,
                            'year': item.year if hasattr(item, 'year') else '',
                            'matched': matched
                        })
                        
                        if len(results) >= 20:  # Limit to 20 results
                            break
            
            if len(results) >= 20:
                break
        
        return jsonify({"results": results})
    except Exception as e:
        print(f"[ERROR] Plex search error: {e}")
        return jsonify({"results": []})

@app.route('/admin/match-content', methods=['POST'])
@require_admin_login
def match_content_manual():
    """Manually match content to TMDb"""
    data = request.json
    plex_id = data.get('plex_id')
    content_type = data.get('type')
    tmdb_id = data.get('tmdb_id')
    
    if not plex_id or not content_type or not tmdb_id:
        return jsonify({"success": False, "error": "Missing parameters"})
    
    try:
        # Fetch TMDb data by ID
        tmdb_type = 'tv' if content_type == 'show' else 'movie'
        url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}"
        params = {
            'api_key': TMDB_API_KEY,
            'append_to_response': 'credits,keywords,videos'
        }
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            tmdb_data = response.json()
            
            # Format TMDb data
            formatted_data = {
                'tmdb_id': tmdb_data.get('id'),
                'poster_path': f"https://image.tmdb.org/t/p/original{tmdb_data.get('poster_path')}" if tmdb_data.get('poster_path') else '',
                'backdrop_path': f"https://image.tmdb.org/t/p/original{tmdb_data.get('backdrop_path')}" if tmdb_data.get('backdrop_path') else '',
                'overview': tmdb_data.get('overview', ''),
                'vote_average': tmdb_data.get('vote_average', 0)
            }
            
            # Save to cache
            cache_key = f"{content_type}_{plex_id}" if content_type == 'movie' else f"series_{plex_id}"
            cache_category = 'movies' if content_type == 'movie' else 'series'
            session_cache[cache_category][cache_key] = formatted_data
            
            # Save to disk
            save_cache_to_disk()
            
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "TMDb API error"})
    except Exception as e:
        print(f"[ERROR] Manual match error: {e}")
        return jsonify({"success": False, "error": str(e)})

@app.route('/admin/logout_old')
def admin_logout_old():
    """Logout"""
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))

@app.route('/admin')
@require_admin_login
def admin_dashboard():
    """Admin dashboard"""
    libraries = []
    server_name = "Not connected"
    
    if plex:
        server_name = plex.friendlyName
        for section in plex.library.sections():
            libraries.append({
                'name': section.title,
                'type': section.type.title(),
                'count': len(section.all())
            })
    
    # Get server IP
    import socket
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)
    bridge_url = f"http://{local_ip}:{BRIDGE_PORT}"
    
    return render_template_string(DASHBOARD_HTML,
        plex_connected=plex is not None,
        tmdb_configured=bool(TMDB_API_KEY),
        server_name=server_name,
        bridge_url=bridge_url,
        bridge_username=BRIDGE_USERNAME,
        bridge_password=BRIDGE_PASSWORD,
        active_sessions=get_active_user_count(),
        libraries=libraries
    )

@app.route('/admin/settings', methods=['GET', 'POST'])
@require_admin_login
def admin_settings():
    """Settings page"""
    global PLEX_URL, PLEX_TOKEN, BRIDGE_USERNAME, BRIDGE_PASSWORD, ADMIN_PASSWORD, SHOW_DUMMY_CHANNEL, TMDB_API_KEY
    
    message = None
    error = False
    
    if request.method == 'POST':
        # Get form data
        new_plex_url = request.form.get('plex_url', '').strip()
        new_plex_token = request.form.get('plex_token', '').strip()
        new_bridge_username = request.form.get('bridge_username', '').strip()
        new_bridge_password = request.form.get('bridge_password', '').strip()
        new_admin_password = request.form.get('admin_password', '').strip()
        new_tmdb_key = request.form.get('tmdb_api_key', '').strip()
        new_show_dummy = request.form.get('show_dummy_channel') == 'on'
        
        # Validate inputs
        if not new_bridge_username or not new_bridge_password:
            message = "✗ Bridge username and password cannot be empty"
            error = True
        elif not new_admin_password:
            message = "✗ Admin password cannot be empty"
            error = True
        else:
            # Update global variables
            PLEX_URL = new_plex_url
            PLEX_TOKEN = new_plex_token
            BRIDGE_USERNAME = new_bridge_username
            BRIDGE_PASSWORD = new_bridge_password
            ADMIN_PASSWORD = new_admin_password
            TMDB_API_KEY = new_tmdb_key
            SHOW_DUMMY_CHANNEL = new_show_dummy
            
            if save_config():
                if PLEX_URL and PLEX_TOKEN:
                    if connect_plex():
                        message = "✓ Settings saved and connected to Plex successfully!"
                    else:
                        message = "⚠️ Settings saved but failed to connect to Plex. Check your URL and token."
                        error = True
                else:
                    message = "✓ Settings saved! Add Plex URL and token to connect to your server."
            else:
                message = "✗ Failed to save settings"
                error = True
    
    return render_template_string(SETTINGS_HTML,
        plex_url=PLEX_URL,
        plex_token=PLEX_TOKEN,
        bridge_username=BRIDGE_USERNAME,
        bridge_password=BRIDGE_PASSWORD,
        admin_password=ADMIN_PASSWORD,
        tmdb_api_key=TMDB_API_KEY,
        show_dummy_channel=SHOW_DUMMY_CHANNEL,
        message=message,
        error=error
    )

@app.route('/admin/test')
@require_admin_login
def admin_test():
    """Test Plex connection"""
    if connect_plex():
        return jsonify({
            "success": True,
            "message": f"Successfully connected to {plex.friendlyName}",
            "version": plex.version,
            "libraries": [s.title for s in plex.library.sections()]
        })
    else:
        return jsonify({
            "success": False,
            "message": "Failed to connect to Plex. Check your URL and token."
        }), 500

# Xtream Codes API Endpoints (keeping all previous API code)

@app.route('/player_api.php')
def player_api():
    """Main Xtream Codes API endpoint"""
    if not plex:
        return jsonify({"error": "Plex server not connected"}), 500
    
    action = request.args.get('action')
    username = request.args.get('username')
    
    # Log the request (helpful for debugging)
    if action:
        print(f"[API] Request: action={action}, user={username}")
    
    if not validate_session():
        return jsonify({
            "user_info": {"auth": 0, "status": "Expired", "message": "Invalid credentials"}
        }), 401
    
    # Authentication endpoint
    if action is None and request.args.get('username') and request.args.get('password'):
        server_info = {
            "url": PLEX_URL,
            "port": BRIDGE_PORT,
            "https_port": "",
            "server_protocol": "http",
            "rtmp_port": "",
            "timezone": "UTC",
            "timestamp_now": int(time.time()),
            "time_now": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        user_info = {
            "username": request.args.get('username'),
            "password": request.args.get('password'),
            "message": "Welcome to Plex Bridge",
            "auth": 1,
            "status": "Active",
            "exp_date": "9999999999",
            "is_trial": "0",
            "active_cons": "1",
            "created_at": str(int(time.time())),
            "max_connections": "5",
            "allowed_output_formats": ["m3u8", "ts"]
        }
        
        return jsonify({
            "user_info": user_info,
            "server_info": server_info
        })
    
    # Get VOD categories - Auto-generated from Plex libraries
    elif action == 'get_vod_categories':
        categories = []
        
        if plex:
            try:
                sections = get_cached_sections()
                for section in sections:
                    if section.type == 'movie':
                        # Each movie library becomes a category
                        categories.append({
                            "category_id": str(section.key),
                            "category_name": section.title,
                            "parent_id": 0
                        })
            except Exception as e:
                print(f"[ERROR] Failed to get VOD categories: {e}")
        
        return jsonify(categories)
    
    # Get VOD streams (movies)
    elif action == 'get_vod_streams':
        category_id = request.args.get('category_id')
        limit = int(request.args.get('limit', 0))  # 0 = no limit (backward compatible)
        
        print(f"[PERF] get_vod_streams: category={category_id}, limit={limit}")
        start_time = time.time()
        
        movies = []
        
        # Handle "All Movies" category (category_id = "0")
        if category_id == "0" or not category_id:
            # No category specified or "All" - return all movies (with limit)
            max_limit = limit if limit > 0 else MAX_MOVIES  # Use environment variable
            count = 0
            
            sections = get_cached_sections()
            for section in sections:
                if section.type == 'movie':
                    # Use search() instead of all() for better performance
                    try:
                        for movie in section.search():
                            if count >= max_limit:
                                break
                            # Fetch TMDb for high-quality posters (cached)
                            formatted = format_movie_for_xtream(movie, section.key, skip_tmdb=False)
                            if formatted:
                                movies.append(formatted)
                                count += 1
                    except Exception as e:
                        print(f"[ERROR] Error iterating movies: {e}")
                
                if count >= max_limit:
                    break
        elif category_id:
            cat_id_str = str(category_id)
            
            # Check if it's a smart category
            smart_cats = get_smart_categories_for_movies()
            smart_cat = next((c for c in smart_cats if c['id'] == cat_id_str), None)
            
            if smart_cat:
                # Get movies from smart category
                movies = get_movies_for_category(smart_cat)
                if limit > 0:
                    movies = movies[:limit]
            else:
                # Check if it's a custom category
                custom_cat = next((c for c in custom_categories.get('movies', []) if c['id'] == cat_id_str), None)
                
                if custom_cat:
                    movies = get_movies_for_category(custom_cat)
                    if limit > 0:
                        movies = movies[:limit]
                else:
                    # Regular Plex library category
                    try:
                        section = plex.library.sectionByID(int(category_id))
                        all_movies = section.all()
                        
                        # Apply limit if specified
                        movies_to_process = all_movies[:limit] if limit > 0 else all_movies
                        
                        for movie in movies_to_process:
                            # Fetch TMDb for high-quality posters (cached)
                            formatted = format_movie_for_xtream(movie, category_id, skip_tmdb=False)
                            if formatted:
                                movies.append(formatted)
                    except Exception as e:
                        print(f"[ERROR] Error getting movies: {e}")
        
        elapsed = time.time() - start_time
        print(f"[PERF] Returned {len(movies)} movies in {elapsed:.2f}s")
        return jsonify(movies)
    
    # Get VOD info
    elif action == 'get_vod_info':
        vod_id = request.args.get('vod_id')
        username = request.args.get('username', 'unknown')
        
        if not vod_id:
            return jsonify({"error": "Missing vod_id"}), 400
        
        # Track that this user is about to stream this content
        track_stream_start(username, vod_id, 'movie')
        
        try:
            movie = plex.fetchItem(int(vod_id))
            stream_url = get_stream_url(movie)
            
            # Get TMDb enhanced metadata
            tmdb_data = enhance_movie_with_tmdb(movie) if TMDB_API_KEY else {}
            
            info = {
                "info": {
                    "tmdb_id": str(tmdb_data.get('tmdb_id', '')) if tmdb_data else "",
                    "imdb_id": tmdb_data.get('imdb_id', '') if tmdb_data else "",
                    "name": movie.title,
                    "o_name": movie.originalTitle or movie.title,
                    "cover_big": tmdb_data.get('backdrop_path', '') if tmdb_data else (f"{PLEX_URL}{movie.art}?X-Plex-Token={PLEX_TOKEN}" if movie.art else ""),
                    "movie_image": tmdb_data.get('poster_path', '') if tmdb_data else (f"{PLEX_URL}{movie.thumb}?X-Plex-Token={PLEX_TOKEN}" if movie.thumb else ""),
                    "releasedate": str(movie.year) if movie.year else "",
                    "youtube_trailer": tmdb_data.get('trailer', '') if tmdb_data else "",
                    "director": tmdb_data.get('director', '') if tmdb_data else (", ".join([d.tag for d in movie.directors]) if movie.directors else ""),
                    "actors": ', '.join([c['name'] for c in tmdb_data.get('cast', [])]) if tmdb_data and tmdb_data.get('cast') else (", ".join([a.tag for a in movie.roles[:10]]) if movie.roles else ""),
                    "cast": ', '.join([c['name'] for c in tmdb_data.get('cast', [])]) if tmdb_data and tmdb_data.get('cast') else (", ".join([a.tag for a in movie.roles[:10]]) if movie.roles else ""),
                    "description": tmdb_data.get('overview', '') if tmdb_data else (movie.summary or ""),
                    "plot": tmdb_data.get('overview', '') if tmdb_data else (movie.summary or ""),
                    "age": movie.contentRating or "",
                    "rating": str(tmdb_data.get('vote_average', movie.rating or 0)),
                    "rating_5based": tmdb_data.get('vote_average', round(float(movie.rating or 0) / 2, 1)),
                    "duration_secs": str(movie.duration // 1000) if movie.duration else "0",
                    "duration": str(movie.duration // 60000) if movie.duration else "0",
                    "genre": ', '.join(tmdb_data.get('genres', [])) if tmdb_data else (", ".join([g.tag for g in movie.genres]) if movie.genres else ""),
                    "backdrop_path": [tmdb_data.get('backdrop_path')] if tmdb_data and tmdb_data.get('backdrop_path') else ([f"{PLEX_URL}{movie.art}?X-Plex-Token={PLEX_TOKEN}"] if movie.art else []),
                    "popularity": tmdb_data.get('popularity', 0) if tmdb_data else 0,
                    "vote_count": tmdb_data.get('vote_count', 0) if tmdb_data else 0,
                    "tagline": tmdb_data.get('tagline', '') if tmdb_data else "",
                    "keywords": ', '.join(tmdb_data.get('keywords', [])) if tmdb_data else ""
                },
                "movie_data": {
                    "stream_id": movie.ratingKey,
                    "name": movie.title,
                    "container_extension": "mkv",
                    "custom_sid": "",
                    "direct_source": stream_url
                }
            }
            return jsonify(info)
        except Exception as e:
            return jsonify({"error": str(e)}), 404
    
    # Get series categories - Auto-generated from Plex libraries
    elif action == 'get_series_categories':
        categories = []
        
        if plex:
            try:
                sections = get_cached_sections()
                for section in sections:
                    if section.type == 'show':
                        # Each TV library becomes a category
                        categories.append({
                            "category_id": str(section.key),
                            "category_name": section.title,
                            "parent_id": 0
                        })
            except Exception as e:
                print(f"[ERROR] Failed to get series categories: {e}")
        
        return jsonify(categories)
    
    # Get series
    elif action == 'get_series':
        category_id = request.args.get('category_id')
        limit = int(request.args.get('limit', 0))  # 0 = no limit
        
        print(f"[PERF] get_series: category={category_id}, limit={limit}")
        start_time = time.time()
        
        series_list = []
        
        # Handle "All Series" category (category_id = "0")
        if category_id == "0" or not category_id:
            # No category specified or "All" - return all series (with limit)
            max_limit = limit if limit > 0 else MAX_SHOWS
            print(f"[DEBUG] Returning all series from all sections (max {max_limit})")
            count = 0
            
            sections = get_cached_sections()
            for section in sections:
                if section.type == 'show':
                    print(f"[DEBUG] Processing TV section: {section.title}")
                    try:
                        for show in section.search():
                            if count >= max_limit:
                                break
                            formatted = format_series_for_xtream(show, section.key)
                            if formatted:
                                series_list.append(formatted)
                                count += 1
                    except Exception as e:
                        print(f"[ERROR] Error iterating shows: {e}")
                
                if count >= max_limit:
                    break
        elif category_id:
            cat_id_str = str(category_id)
            
            # Check if it's a smart category
            smart_cats = get_smart_categories_for_series()
            smart_cat = next((c for c in smart_cats if c['id'] == cat_id_str), None)
            
            if smart_cat:
                # Get series from smart category
                print(f"[DEBUG] Using smart category: {smart_cat['name']}")
                series_list = get_series_for_category(smart_cat)
                if limit > 0:
                    series_list = series_list[:limit]
            else:
                # Check if it's a custom category
                custom_cat = next((c for c in custom_categories.get('series', []) if c['id'] == cat_id_str), None)
                
                if custom_cat:
                    print(f"[DEBUG] Using custom category")
                    series_list = get_series_for_category(custom_cat)
                    if limit > 0:
                        series_list = series_list[:limit]
                else:
                    # Regular Plex library category
                    try:
                        section = plex.library.sectionByID(int(category_id))
                        print(f"[DEBUG] Using Plex section: {section.title}")
                        all_shows = section.all()
                        
                        # Apply limit if specified
                        shows_to_process = all_shows[:limit] if limit > 0 else all_shows
                        
                        for show in shows_to_process:
                            formatted = format_series_for_xtream(show, category_id)
                            if formatted:
                                series_list.append(formatted)
                    except Exception as e:
                        print(f"[ERROR] Error getting series: {e}")
        else:
            # No category specified - return all series (with optional limit)
            # Default to 300 max to prevent timeout
            max_limit = limit if limit > 0 else MAX_SHOWS
            print(f"[DEBUG] Returning all series from all sections (max {max_limit})")
            count = 0
            
            sections = get_cached_sections()
            for section in sections:
                if section.type == 'show':
                    print(f"[DEBUG] Processing TV section: {section.title}")
                    try:
                        for show in section.search():
                            if count >= max_limit:
                                break
                            formatted = format_series_for_xtream(show, section.key)
                            if formatted:
                                series_list.append(formatted)
                                count += 1
                    except Exception as e:
                        print(f"[ERROR] Error iterating shows: {e}")
                
                if count >= max_limit:
                    break
        
        elapsed = time.time() - start_time
        print(f"[PERF] Returned {len(series_list)} TV shows in {elapsed:.2f}s")
        return jsonify(series_list)
    
    # Get live categories (for Live TV)
    elif action == 'get_live_categories':
        categories = []
        
        # Check if there are any Live TV sections in Plex
        has_plex_livetv = False
        try:
            for section in plex.library.sections():
                if section.type == 'livetv':
                    has_plex_livetv = True
                    categories.append({
                        "category_id": section.key,
                        "category_name": section.title,
                        "parent_id": 0
                    })
        except:
            pass
        
        # If no Plex Live TV, add a dummy category so players don't error
        if not has_plex_livetv:
            categories.append({
                "category_id": "999",
                "category_name": "Plex Bridge Info",
                "parent_id": 0
            })
        
        return jsonify(categories)
    
    # Get live streams (for Live TV)
    elif action == 'get_live_streams':
        category_id = request.args.get('category_id')
        streams = []
        
        # Check for real Plex Live TV
        has_plex_livetv = False
        try:
            for section in plex.library.sections():
                if section.type == 'livetv':
                    has_plex_livetv = True
                    # Here you could add real live TV channels from Plex
                    # For now, we'll leave it empty if they exist
        except:
            pass
        
        # If no Plex Live TV and dummy channels are enabled, add info channels
        if not has_plex_livetv and SHOW_DUMMY_CHANNEL and (category_id == "999" or category_id is None):
            # Add a dummy channel with useful info
            streams.append({
                "num": 1,
                "name": "📺 Plex Bridge - Info Channel",
                "stream_type": "live",
                "stream_id": 99999,
                "stream_icon": f"{PLEX_URL}/:/resources/plex-icon-120.png" if PLEX_URL else "",
                "epg_channel_id": "",
                "added": str(int(time.time())),
                "category_id": "999",
                "custom_sid": "",
                "tv_archive": 0,
                "direct_source": "",
                "tv_archive_duration": 0
            })
            
            # Optionally add more informational "channels"
            if plex:
                movie_count = sum(1 for s in plex.library.sections() if s.type == 'movie')
                show_count = sum(1 for s in plex.library.sections() if s.type == 'show')
                
                streams.append({
                    "num": 2,
                    "name": f"📊 Library Stats: {movie_count} Movie Libraries, {show_count} TV Libraries",
                    "stream_type": "live",
                    "stream_id": 99998,
                    "stream_icon": "",
                    "epg_channel_id": "",
                    "added": str(int(time.time())),
                    "category_id": "999",
                    "custom_sid": "",
                    "tv_archive": 0,
                    "direct_source": "",
                    "tv_archive_duration": 0
                })
        
        return jsonify(streams)
    
    # Get EPG (Electronic Program Guide)
    elif action == 'get_simple_data_table' or action == 'get_epg':
        return jsonify([])
    
    # Get short EPG
    elif action == 'get_short_epg':
        stream_id = request.args.get('stream_id')
        return jsonify({"epg_listings": []})
    
    # Get all EPG
    elif action == 'get_all_epg':
        return jsonify([])
    
    # Get series info
    elif action == 'get_series_info':
        series_id = request.args.get('series_id')
        username = request.args.get('username', 'unknown')
        
        if not series_id:
            return jsonify({"error": "Missing series_id"}), 400
        
        # Track that this user is browsing/about to stream this series
        track_stream_start(username, series_id, 'series')
        
        try:
            print(f"[DEBUG] Attempting to fetch series ID: {series_id}")
            
            # Try to fetch the item
            try:
                show = plex.fetchItem(int(series_id))
            except Exception as fetch_error:
                print(f"[ERROR] Failed to fetch item {series_id}: {fetch_error}")
                # Return empty structure instead of 404
                return jsonify({
                    "seasons": [],
                    "info": {
                        "name": f"Series {series_id}",
                        "cover": "",
                        "plot": "Unable to load series information",
                        "cast": "",
                        "director": "",
                        "genre": "",
                        "releaseDate": "",
                        "rating": "0",
                        "rating_5based": 0,
                        "backdrop_path": [],
                        "youtube_trailer": "",
                        "episode_run_time": "",
                        "category_id": "2"
                    },
                    "episodes": {}
                })
            
            print(f"[DEBUG] Getting series info for: {show.title} (Type: {show.type})")
            
            # Check if it's actually a show
            if show.type != 'show':
                print(f"[WARNING] Item {series_id} is not a show, it's a {show.type}")
                return jsonify({"error": f"Item is not a TV show, it's a {show.type}"}), 400
            
            seasons = []
            episodes_data = {}
            
            for season in show.seasons():
                season_num = season.seasonNumber
                if season_num is None:
                    print(f"[DEBUG] Skipping season with no number")
                    continue
                
                print(f"[DEBUG] Processing season {season_num}: {season.title}")
                    
                season_info = {
                    "air_date": str(season.year) if hasattr(season, 'year') and season.year else "",
                    "episode_count": len(season.episodes()),
                    "id": season.ratingKey,
                    "name": season.title,
                    "overview": season.summary or "",
                    "season_number": season_num,
                    "cover": f"{PLEX_URL}{season.thumb}?X-Plex-Token={PLEX_TOKEN}" if season.thumb else "",
                    "cover_big": f"{PLEX_URL}{season.art}?X-Plex-Token={PLEX_TOKEN}" if hasattr(season, 'art') and season.art else ""
                }
                
                seasons.append(season_info)
                
                episodes = []
                for episode in season.episodes():
                    formatted_episode = format_episode_for_xtream(episode, series_id)
                    if formatted_episode:
                        episodes.append(formatted_episode)
                
                print(f"[DEBUG] Season {season_num} has {len(episodes)} episodes")
                
                if episodes:
                    # Use string key for JSON compatibility
                    episodes_data[str(season_num)] = episodes
            
            print(f"[DEBUG] Total seasons: {len(seasons)}, Total episode groups: {len(episodes_data)}")
            
            info = {
                "seasons": seasons,
                "info": {
                    "name": show.title,
                    "cover": f"{PLEX_URL}{show.thumb}?X-Plex-Token={PLEX_TOKEN}" if hasattr(show, 'thumb') and show.thumb else "",
                    "plot": show.summary if hasattr(show, 'summary') else "",
                    "cast": ", ".join([actor.tag for actor in show.roles[:10]]) if hasattr(show, 'roles') and show.roles else "",
                    "director": ", ".join([d.tag for d in show.directors]) if hasattr(show, 'directors') and show.directors else "",
                    "genre": ", ".join([g.tag for g in show.genres]) if hasattr(show, 'genres') and show.genres else "",
                    "releaseDate": str(show.year) if hasattr(show, 'year') and show.year else "",
                    "rating": str(show.rating) if hasattr(show, 'rating') and show.rating else "0",
                    "rating_5based": round(float(show.rating or 0) / 2, 1) if hasattr(show, 'rating') else 0,
                    "backdrop_path": [f"{PLEX_URL}{show.art}?X-Plex-Token={PLEX_TOKEN}"] if hasattr(show, 'art') and show.art else [],
                    "youtube_trailer": "",
                    "episode_run_time": "",
                    "category_id": "2"
                },
                "episodes": episodes_data
            }
            
            return jsonify(info)
        except Exception as e:
            print(f"[ERROR] Error getting series info: {e}")
            import traceback
            traceback.print_exc()
            # Return 200 with empty data instead of 404
            return jsonify({
                "seasons": [],
                "info": {
                    "name": "Error loading series",
                    "cover": "",
                    "plot": str(e),
                    "cast": "",
                    "director": "",
                    "genre": "",
                    "releaseDate": "",
                    "rating": "0",
                    "rating_5based": 0,
                    "backdrop_path": [],
                    "youtube_trailer": "",
                    "episode_run_time": "",
                    "category_id": "2"
                },
                "episodes": {}
            })
    
    return jsonify({"error": "Unknown action"}), 400

@app.route('/movie/<username>/<password>/<stream_id>.mkv')
@app.route('/movie/<username>/<password>/<stream_id>.mp4')
def stream_movie(username, password, stream_id):
    """Stream a movie"""
    if not authenticate(username, password):
        return "Unauthorized", 401
    
    # Track this stream
    track_stream_start(username, stream_id, 'movie')
    
    try:
        movie = plex.fetchItem(int(stream_id))
        stream_url = get_stream_url(movie)
        if stream_url:
            return Response(
                status=302,
                headers={'Location': stream_url}
            )
        return "Stream not found", 404
    except Exception as e:
        return str(e), 404

@app.route('/series/<username>/<password>/<stream_id>.mkv')
@app.route('/series/<username>/<password>/<stream_id>.mp4')
def stream_episode(username, password, stream_id):
    """Stream a TV episode"""
    if not authenticate(username, password):
        print(f"[AUTH] Unauthorized attempt for episode {stream_id}")
        return "Unauthorized", 401
    
    # Track this stream
    track_stream_start(username, stream_id, 'episode')
    
    try:
        print(f"[STREAM] Fetching episode ID: {stream_id}")
        episode = plex.fetchItem(int(stream_id))
        print(f"[STREAM] Found episode: {episode.title} (S{episode.seasonNumber}E{episode.index})")
        
        stream_url = get_stream_url(episode)
        if stream_url:
            print(f"[STREAM] Redirecting to: {stream_url[:100]}...")
            return Response(
                status=302,
                headers={'Location': stream_url}
            )
        
        print(f"[ERROR] No stream URL generated for episode {stream_id}")
        return "Stream not found", 404
    except Exception as e:
        print(f"[ERROR] Error streaming episode {stream_id}: {e}")
        import traceback
        traceback.print_exc()
        return str(e), 404

@app.route('/admin/category-editor')
@require_admin_login
def category_editor():
    """Advanced category editor with filters"""
    editor_html = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Category Editor - Plex Xtream Bridge</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        .card {
            background: white;
            padding: 30px;
            border-radius: 15px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            margin-bottom: 20px;
        }
        .button {
            padding: 10px 20px;
            background: #667eea;
            color: white;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            text-decoration: none;
            display: inline-block;
        }
        .button:hover { background: #5568d3; }
        .button-secondary { background: #6c757d; }
        .button-success { background: #28a745; }
        .button-danger { background: #dc3545; }
        .code-editor {
            background: #2d2d2d;
            color: #f8f8f2;
            padding: 20px;
            border-radius: 8px;
            font-family: 'Courier New', monospace;
            margin: 20px 0;
            min-height: 200px;
        }
        textarea {
            width: 100%;
            min-height: 300px;
            background: #2d2d2d;
            color: #f8f8f2;
            border: 2px solid #444;
            border-radius: 8px;
            padding: 15px;
            font-family: 'Courier New', monospace;
            font-size: 14px;
        }
        .help-box {
            background: #e7f3ff;
            padding: 20px;
            border-radius: 8px;
            margin: 20px 0;
            border-left: 4px solid #2196F3;
        }
        .example-code {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 5px;
            margin: 10px 0;
            border-left: 3px solid #667eea;
        }
        pre { margin: 10px 0; }
        code {
            background: #2d2d2d;
            color: #f8f8f2;
            padding: 2px 6px;
            border-radius: 3px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h1>🎬 Advanced Category Editor</h1>
            <p style="color: #666; margin: 10px 0 20px 0;">Create custom categories with code-based filters</p>
            <a href="/admin/categories" class="button button-secondary">← Back to Categories</a>
        </div>
        
        <div class="card">
            <h2>📝 Create Custom Category</h2>
            <div class="help-box">
                <h3 style="margin-bottom: 10px;">How It Works:</h3>
                <p>Write Python-like filter code to select movies/shows. Your code has access to:</p>
                <ul style="margin-left: 25px; margin-top: 10px; line-height: 1.8;">
                    <li><code>title</code> - Movie/show title</li>
                    <li><code>year</code> - Release year</li>
                    <li><code>rating</code> - Rating (0-10)</li>
                    <li><code>genre</code> - List of genres</li>
                    <li><code>director</code> - Director name(s)</li>
                    <li><code>cast</code> - List of actors</li>
                    <li><code>added_date</code> - Date added to Plex</li>
                </ul>
            </div>
            
            <h3 style="margin: 20px 0 10px 0;">Examples:</h3>
            
            <div class="example-code">
                <strong>🎬 90s Action Movies:</strong>
                <pre><code>year >= 1990 and year < 2000 and 'Action' in genre</code></pre>
            </div>
            
            <div class="example-code">
                <strong>⭐ Highly Rated Sci-Fi:</strong>
                <pre><code>rating >= 8.0 and 'Science Fiction' in genre</code></pre>
            </div>
            
            <div class="example-code">
                <strong>🎭 Christopher Nolan Films:</strong>
                <pre><code>'Christopher Nolan' in director</code></pre>
            </div>
            
            <div class="example-code">
                <strong>🎪 Tom Hanks Movies:</strong>
                <pre><code>'Tom Hanks' in cast</code></pre>
            </div>
            
            <div class="example-code">
                <strong>📅 Recently Added (last 30 days):</strong>
                <pre><code>days_since_added <= 30</code></pre>
            </div>
            
            <form method="POST" action="/admin/create-category" style="margin-top: 30px;">
                <div style="margin-bottom: 15px;">
                    <label style="display: block; font-weight: 600; margin-bottom: 5px;">Category Name:</label>
                    <input type="text" name="category_name" required 
                           placeholder="My Custom Category" 
                           style="width: 100%; padding: 10px; border: 2px solid #e1e4e8; border-radius: 5px;">
                </div>
                
                <div style="margin-bottom: 15px;">
                    <label style="display: block; font-weight: 600; margin-bottom: 5px;">Type:</label>
                    <select name="category_type" style="width: 100%; padding: 10px; border: 2px solid #e1e4e8; border-radius: 5px;">
                        <option value="movies">Movies</option>
                        <option value="series">TV Shows</option>
                    </select>
                </div>
                
                <div style="margin-bottom: 15px;">
                    <label style="display: block; font-weight: 600; margin-bottom: 5px;">Filter Code:</label>
                    <textarea name="filter_code" placeholder="rating >= 8.0 and year >= 2020" required></textarea>
                </div>
                
                <div style="margin-bottom: 15px;">
                    <label style="display: block; font-weight: 600; margin-bottom: 5px;">Maximum Items:</label>
                    <input type="number" name="max_items" value="100" min="10" max="500"
                           style="width: 200px; padding: 10px; border: 2px solid #e1e4e8; border-radius: 5px;">
                </div>
                
                <button type="submit" class="button button-success">✅ Create Category</button>
            </form>
        </div>
        
        <div class="card">
            <h2>📚 Your Custom Categories</h2>
            <p style="color: #666; margin-bottom: 20px;">Manage your code-based categories</p>
            
            <div id="custom-categories">
                <!-- Will be populated via JavaScript or template -->
                <p style="color: #999;">No custom categories yet. Create one above!</p>
            </div>
        </div>
    </div>
</body>
</html>
    """
    return render_template_string(editor_html)

@app.route('/admin/category/<category_type>/<category_id>')
@require_admin_login
def view_category_contents(category_type, category_id):
    """Category viewer disabled - redirect to dashboard"""
    return redirect('/admin')
    """View all content in a specific category"""
    
    # Get the category details
    if category_type == 'movie':
        smart_cats = get_smart_categories_for_movies()
        custom_cats = custom_categories.get('movies', [])
    else:
        smart_cats = get_smart_categories_for_series()
        custom_cats = custom_categories.get('series', [])
    
    # Find the category
    category = None
    cat_id_str = str(category_id)
    
    # Check smart categories
    category = next((c for c in smart_cats if c['id'] == cat_id_str), None)
    
    # Check custom categories if not found
    if not category:
        category = next((c for c in custom_cats if c['id'] == cat_id_str), None)
    
    # Check Plex library categories
    if not category and plex:
        try:
            section = plex.library.sectionByID(int(category_id))
            category = {
                'id': category_id,
                'name': f"📁 {section.title}",
                'type': 'plex_library',
                'section_id': category_id
            }
        except:
            pass
    
    if not category:
        return "Category not found", 404
    
    # Get content for this category
    if category_type == 'movie':
        if category['type'] == 'plex_library':
            # Get all movies from library
            try:
                section = plex.library.sectionByID(int(category_id))
                items = section.all()
            except:
                items = []
        else:
            # Get from category function
            temp_items = get_movies_for_category(category)
            # Extract just the titles and IDs
            items = []
            for formatted in temp_items:
                try:
                    movie = plex.fetchItem(int(formatted['stream_id']))
                    items.append(movie)
                except:
                    pass
    else:
        if category['type'] == 'plex_library':
            # Get all shows from library
            try:
                section = plex.library.sectionByID(int(category_id))
                items = section.all()
            except:
                items = []
        else:
            # Get from category function
            temp_items = get_series_for_category(category)
            # Extract just the titles and IDs
            items = []
            for formatted in temp_items:
                try:
                    show = plex.fetchItem(int(formatted['series_id']))
                    items.append(show)
                except:
                    pass
    
    # Build HTML
    category_view_html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{category['name']} - Contents</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        .card {{
            background: white;
            padding: 30px;
            border-radius: 15px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            margin-bottom: 20px;
        }}
        .button {{
            padding: 10px 20px;
            background: #667eea;
            color: white;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            text-decoration: none;
            display: inline-block;
        }}
        .button:hover {{ background: #5568d3; }}
        .button-secondary {{ background: #6c757d; }}
        .item-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
            gap: 20px;
            margin-top: 20px;
        }}
        .item-card {{
            background: #f8f9fa;
            padding: 15px;
            border-radius: 10px;
            text-align: center;
            transition: transform 0.2s;
        }}
        .item-card:hover {{
            transform: translateY(-5px);
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
        }}
        .item-poster {{
            width: 100%;
            height: 280px;
            object-fit: cover;
            border-radius: 8px;
            margin-bottom: 10px;
            background: #e1e4e8;
        }}
        .item-title {{
            font-weight: 600;
            color: #333;
            margin-bottom: 5px;
            font-size: 14px;
        }}
        .item-year {{
            color: #666;
            font-size: 12px;
        }}
        .item-rating {{
            color: #f39c12;
            font-size: 12px;
            margin-top: 5px;
        }}
        .stats {{
            background: #e7f3ff;
            padding: 15px;
            border-radius: 8px;
            margin: 20px 0;
            display: flex;
            gap: 30px;
            flex-wrap: wrap;
        }}
        .stat {{
            flex: 1;
            min-width: 150px;
        }}
        .stat-value {{
            font-size: 32px;
            font-weight: bold;
            color: #667eea;
        }}
        .stat-label {{
            color: #666;
            font-size: 14px;
        }}
        .search-box {{
            width: 100%;
            padding: 12px;
            border: 2px solid #e1e4e8;
            border-radius: 8px;
            font-size: 14px;
            margin-bottom: 20px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h1>{category['name']}</h1>
            <p style="color: #666; margin: 10px 0 20px 0;">Category Details</p>
            <a href="/admin/categories" class="button button-secondary">← Back to Categories</a>
        </div>
        
        <div class="card">
            <div class="stats">
                <div class="stat">
                    <div class="stat-value">{len(items)}</div>
                    <div class="stat-label">Total Items</div>
                </div>
                <div class="stat">
                    <div class="stat-value">{category.get('type', 'N/A').replace('_', ' ').title()}</div>
                    <div class="stat-label">Category Type</div>
                </div>
            </div>
            
            <input type="text" class="search-box" id="searchBox" placeholder="🔍 Search by title..." onkeyup="filterItems()">
            
            <div class="item-grid" id="itemGrid">
"""
    
    # Add items
    for item in items[:200]:  # Limit to 200 for performance
        try:
            title = item.title
            year = item.year if hasattr(item, 'year') and item.year else 'N/A'
            rating = round(item.rating, 1) if hasattr(item, 'rating') and item.rating else 'N/A'
            
            # Get poster
            if hasattr(item, 'thumb') and item.thumb:
                poster = f"{PLEX_URL}{item.thumb}?X-Plex-Token={PLEX_TOKEN}"
            else:
                poster = ""
            
            category_view_html += f"""
                <div class="item-card" data-title="{title.lower()}">
                    {f'<img src="{poster}" class="item-poster" alt="{title}">' if poster else '<div class="item-poster"></div>'}
                    <div class="item-title">{title}</div>
                    <div class="item-year">{year}</div>
                    {f'<div class="item-rating">⭐ {rating}</div>' if rating != 'N/A' else ''}
                </div>
"""
        except Exception as e:
            print(f"Error formatting item: {e}")
            continue
    
    category_view_html += """
            </div>
        </div>
    </div>
    
    <script>
        function filterItems() {
            const searchTerm = document.getElementById('searchBox').value.toLowerCase();
            const items = document.querySelectorAll('.item-card');
            
            items.forEach(item => {
                const title = item.getAttribute('data-title');
                if (title.includes(searchTerm)) {
                    item.style.display = '';
                } else {
                    item.style.display = 'none';
                }
            });
        }
    </script>
</body>
</html>
"""
    
    return render_template_string(category_view_html)

@app.route('/admin/categories')
@require_admin_login
def admin_categories():
    """Categories disabled - redirect to dashboard"""
    return redirect('/admin')

@app.route('/admin/categories/create', methods=['GET', 'POST'])
@require_admin_login
def create_category():
    """Categories disabled - redirect to dashboard"""
    return redirect('/admin')

@app.route('/admin/categories/<cat_id>/view')
@require_admin_login
def view_category(cat_id):
    """Categories disabled - redirect to dashboard"""
    return redirect('/admin')

@app.route('/admin/categories/<cat_id>/delete', methods=['POST'])
@require_admin_login
def delete_category(cat_id):
    """Categories disabled - redirect to dashboard"""
    return redirect('/admin')

# Original categories page function (disabled)
def admin_categories_old():
    """Categories management page"""
    # Get smart categories
    movie_smart = get_smart_categories_for_movies()
    series_smart = get_smart_categories_for_series()
    
    # Get custom categories
    movie_custom = custom_categories.get('movies', [])
    series_custom = custom_categories.get('series', [])
    
    # Get Plex sections for creating new categories
    plex_movie_sections = []
    plex_tv_sections = []
    
    if plex:
        plex_movie_sections = [{'id': s.key, 'name': s.title} for s in plex.library.sections() if s.type == 'movie']
        plex_tv_sections = [{'id': s.key, 'name': s.title} for s in plex.library.sections() if s.type == 'show']
    
    categories_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Categories Management - Plex Xtream Bridge</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        
        .card {
            background: white;
            padding: 30px;
            border-radius: 15px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.2);
            margin-bottom: 20px;
        }
        
        .card h1, .card h2 {
            color: #333;
            margin-bottom: 15px;
        }
        
        .button {
            display: inline-block;
            padding: 12px 24px;
            background: #667eea;
            color: white;
            text-decoration: none;
            border-radius: 8px;
            font-weight: 600;
            border: none;
            cursor: pointer;
            transition: background 0.3s;
        }
        
        .button:hover {
            background: #5568d3;
        }
        
        .button-secondary {
            background: #6c757d;
        }
        
        .category-list {
            display: grid;
            gap: 10px;
            margin-top: 15px;
        }
        
        .category-item {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 8px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .category-info {
            flex: 1;
        }
        
        .category-name {
            font-weight: 600;
            color: #333;
        }
        
        .category-badge {
            background: #667eea;
            color: white;
            padding: 4px 10px;
            border-radius: 12px;
            font-size: 12px;
            margin-left: 10px;
        }
        
        .view-button {
            padding: 8px 16px;
            background: #28a745;
            color: white;
            text-decoration: none;
            border-radius: 6px;
            font-size: 13px;
            font-weight: 600;
            transition: background 0.2s;
        }
        
        .view-button:hover {
            background: #218838;
        }
        
        .info-box {
            background: #e7f3ff;
            border-left: 4px solid #2196F3;
            padding: 15px;
            border-radius: 5px;
            margin: 15px 0;
        }
        
        .success-box {
            background: #d4edda;
            border-left: 4px solid #28a745;
            padding: 15px;
            border-radius: 5px;
            margin: 15px 0;
            color: #155724;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <h1>📚 Categories Management</h1>
            <p style="color: #666; margin-bottom: 20px;">Manage your movie and TV show categories</p>
            
            <a href="/admin" class="button button-secondary">← Back to Dashboard</a>
        </div>
        
        <div class="card">
            <h2>🎬 Movie Categories</h2>
            
            <div class="info-box">
                <strong>Auto-Generated Categories</strong><br>
                These are automatically created from your Plex libraries and include Recently Added, Recently Released, Highly Rated, and Genre-based categories.
            </div>
            
            <div class="category-list">
                {% for cat in movie_smart %}
                <div class="category-item">
                    <div class="category-info">
                        <span class="category-name">{{ cat.name }}</span>
                        <span class="category-badge">Auto</span>
                    </div>
                    <a href="/admin/category/movie/{{ cat.id }}" class="view-button">👁️ View Contents</a>
                </div>
                {% endfor %}
            </div>
            
            {% if movie_custom %}
            <h3 style="margin-top: 30px;">Custom Categories</h3>
            <div class="category-list">
                {% for cat in movie_custom %}
                <div class="category-item">
                    <div class="category-info">
                        <span class="category-name">{{ cat.name }}</span>
                        <span class="category-badge" style="background: #28a745;">Custom</span>
                    </div>
                    <a href="/admin/category/movie/{{ cat.id }}" class="view-button">👁️ View Contents</a>
                </div>
                {% endfor %}
            </div>
            {% endif %}
        </div>
        
        <div class="card">
            <h2>📺 TV Show Categories</h2>
            
            <div class="info-box">
                <strong>Auto-Generated Categories</strong><br>
                These are automatically created from your Plex libraries and include Recently Added, Recently Aired, Highly Rated, and Genre-based categories.
            </div>
            
            <div class="category-list">
                {% for cat in series_smart %}
                <div class="category-item">
                    <div class="category-info">
                        <span class="category-name">{{ cat.name }}</span>
                        <span class="category-badge">Auto</span>
                    </div>
                    <a href="/admin/category/series/{{ cat.id }}" class="view-button">👁️ View Contents</a>
                </div>
                {% endfor %}
            </div>
            
            {% if series_custom %}
            <h3 style="margin-top: 30px;">Custom Categories</h3>
            <div class="category-list">
                {% for cat in series_custom %}
                <div class="category-item">
                    <div class="category-info">
                        <span class="category-name">{{ cat.name }}</span>
                        <span class="category-badge" style="background: #28a745;">Custom</span>
                    </div>
                    <a href="/admin/category/series/{{ cat.id }}" class="view-button">👁️ View Contents</a>
                </div>
                {% endfor %}
            </div>
            {% endif %}
        </div>
        
        <div class="card">
            <h2>ℹ️ About Categories</h2>
            
            <div class="success-box">
                <strong>✅ Categories are now active!</strong><br>
                Your Xtream UI player will show all these categories automatically. No need to do anything - just refresh your player app!
            </div>
            
            <h3 style="margin-top: 20px;">What's Included:</h3>
            <ul style="margin-left: 25px; margin-top: 10px; color: #666; line-height: 1.8;">
                <li><strong>🆕 Recently Added</strong> - Your newest content (last 50 items)</li>
                <li><strong>🎬 Recently Released</strong> - Content sorted by release date</li>
                <li><strong>⭐ Highly Rated</strong> - Your best-rated content</li>
                <li><strong>🎭 Genre Categories</strong> - Automatically created for each genre in your library</li>
            </ul>
            
            <h3 style="margin-top: 20px;">Coming Soon:</h3>
            <ul style="margin-left: 25px; margin-top: 10px; color: #666; line-height: 1.8;">
                <li>Create custom categories with your own filters</li>
                <li>TMDb integration for enhanced metadata</li>
                <li>Year-based categories</li>
                <li>Decade collections</li>
                <li>Actor/Director based categories</li>
            </ul>
        </div>
    </div>
</body>
</html>
    """
    
    return render_template_string(categories_html,
        movie_smart=movie_smart,
        series_smart=series_smart,
        movie_custom=movie_custom,
        series_custom=series_custom,
        plex_movie_sections=plex_movie_sections,
        plex_tv_sections=plex_tv_sections
    )

@app.route('/')
def index():
    """Root endpoint - redirect to admin"""
    return redirect(url_for('admin_dashboard'))

if __name__ == '__main__':
    # Register signal handlers for graceful shutdown
    def signal_handler(sig, frame):
        print("\n[SHUTDOWN] Saving cache before exit...")
        save_cache_to_disk()
        print("[SHUTDOWN] Cache saved. Exiting.")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    print("=" * 60)
    print("Plex to Xtream Codes API Bridge with Web Interface")
    print("=" * 60)
    print(f"Web Interface: http://{BRIDGE_HOST}:{BRIDGE_PORT}/admin")
    print(f"Default Admin Password: {ADMIN_PASSWORD}")
    print(f"API Endpoint: http://{BRIDGE_HOST}:{BRIDGE_PORT}/player_api.php")
    print(f"Bridge Username: {BRIDGE_USERNAME}")
    print(f"Bridge Password: {BRIDGE_PASSWORD}")
    print("=" * 60)
    print("\n🔒 Security Features:")
    print("  • API keys and tokens are encrypted")
    print("  • Passwords are hashed with SHA-256")
    print("  • Config files have restricted permissions")
    print("  • First-time password change required")
    print("=" * 60)
    
    if ADMIN_PASSWORD == 'admin123':
        print("\n⚠️  IMPORTANT: You'll be asked to change the default password on first login!")
    
    print("=" * 60)
    
    # Load TMDb cache from disk
    print("\n💾 Loading TMDb cache...")
    load_cache_from_disk()
    
    # Run with optimized settings for multiple concurrent users
    print("\n⚡ Optimized for multi-user performance")
    print("  • Cached library sections (5-minute refresh)")
    print("  • Minimal response size for fast loading")
    print("  • Threaded request handling")
    print(f"  • Max movies: {MAX_MOVIES}")
    print(f"  • Max TV shows: {MAX_SHOWS}")
    
    # Start background auto-matcher (only if TMDb is configured)
    if TMDB_API_KEY and plex:
        print("\n🎬 Starting TMDb auto-matcher")
        print("  • Running initial match on startup")
        print("  • Then runs every 30 minutes")
        print("  • Auto-matches unmatched content")
        auto_matcher_thread = threading.Thread(target=background_auto_matcher, daemon=True)
        auto_matcher_thread.start()
    else:
        if not TMDB_API_KEY:
            print("\n⚠️  TMDb API key not configured - using Plex posters only")
            print("  • Add TMDb API key in Settings to enable high-quality posters")
    
    print("=" * 60)
    
    # Run with threaded=True for better concurrent user handling
    app.run(host=BRIDGE_HOST, port=BRIDGE_PORT, debug=False, threaded=True, processes=1)
