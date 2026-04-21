#!/usr/bin/env python3
"""
Plex to Xtream Codes API Bridge with Web Interface
Allows Xtream UI players to access Plex library content with easy configuration
"""

from flask import Flask, jsonify, request, Response, render_template_string, redirect, url_for, session
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

# ─────────────────────────────────────────────────────────────────────────────
# PlexClient — raw HTTP interface to Plex Media Server
# Replaces plexapi with direct requests calls for consistency with the
# future Emby/Jellyfin abstraction layer.
# ─────────────────────────────────────────────────────────────────────────────

class PlexClient:
    """
    Thin HTTP client for Plex Media Server.
    All methods return normalized dicts so the rest of the app never
    touches Plex-specific object types.
    """

    def __init__(self, url, token):
        self.url         = url.rstrip('/')
        self.token       = token
        self.server_name = ''
        self.machine_id  = ''
        self._session    = requests.Session()
        self._session.headers.update({
            'X-Plex-Token':          token,
            'X-Plex-Client-Identifier': 'plex-xtream-bridge',
            'Accept':                'application/json',
        })

    # ── Connection ──────────────────────────────────────────────────────────

    def connect(self):
        """Test connection and populate server_name / machine_id."""
        try:
            r = self._get('/')
            self.server_name = r.get('MediaContainer', {}).get('friendlyName', '')
            self.machine_id  = r.get('MediaContainer', {}).get('machineIdentifier', '')
            return True
        except Exception as e:
            print(f"[PLEX] Connection failed: {e}")
            return False

    def _get(self, path, params=None):
        url = self.url + path
        r   = self._session.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    # ── Libraries ───────────────────────────────────────────────────────────

    def get_libraries(self):
        """Return list of {'id', 'title', 'type'} dicts."""
        data = self._get('/library/sections')
        libs = []
        for d in data.get('MediaContainer', {}).get('Directory', []):
            libs.append({
                'id':    d['key'],
                'title': d['title'],
                'type':  d['type'],   # 'movie' or 'show'
            })
        return libs

    def total_view_size(self, lib_id, media_type='movie'):
        """Return total item count for a library section."""
        try:
            plex_type = 1 if media_type == 'movie' else 2
            data = self._get(f'/library/sections/{lib_id}/all',
                             params={'type': plex_type, 'X-Plex-Container-Size': 0,
                                     'X-Plex-Container-Start': 0})
            return data.get('MediaContainer', {}).get('totalSize', 0)
        except Exception:
            return 0

    # ── Items ────────────────────────────────────────────────────────────────

    def _normalize_movie(self, d):
        """Normalize a Plex Metadata dict to our common item format."""
        media  = d.get('Media', [{}])[0]
        part   = media.get('Part', [{}])[0]
        genres = [g['tag'] for g in d.get('Genre', [])]
        return {
            'id':             str(d.get('ratingKey', '')),
            'title':          d.get('title', ''),
            'year':           d.get('year'),
            'summary':        d.get('summary', ''),
            'rating':         d.get('rating'),
            'content_rating': d.get('contentRating', ''),
            'thumb':          f"{self.url}{d['thumb']}?X-Plex-Token={self.token}" if d.get('thumb') else '',
            'art':            f"{self.url}{d['art']}?X-Plex-Token={self.token}"   if d.get('art')   else '',
            'genres':         genres,
            'added_at':       d.get('addedAt', 0),
            'duration':       d.get('duration', 0),
            'view_offset':    d.get('viewOffset', 0),
            'type':           'movie',
            'media_parts':    [{'key': part.get('key', ''), 'container': part.get('container', 'mkv')}],
            'directors':      [c['tag'] for c in d.get('Director', [])],
            'roles':          [c['tag'] for c in d.get('Role', [])[:10]],
            'original_title': d.get('originalTitle', d.get('title', '')),
        }

    def _normalize_show(self, d):
        genres = [g['tag'] for g in d.get('Genre', [])]
        return {
            'id':          str(d.get('ratingKey', '')),
            'title':       d.get('title', ''),
            'year':        d.get('year'),
            'summary':     d.get('summary', ''),
            'rating':      d.get('rating'),
            'thumb':       f"{self.url}{d['thumb']}?X-Plex-Token={self.token}" if d.get('thumb') else '',
            'art':         f"{self.url}{d['art']}?X-Plex-Token={self.token}"   if d.get('art')   else '',
            'genres':      genres,
            'added_at':    d.get('addedAt', 0),
            'duration':    d.get('duration', 0),
            'type':        'show',
            'directors':   [c['tag'] for c in d.get('Director', [])],
            'roles':       [c['tag'] for c in d.get('Role', [])[:10]],
        }

    def _normalize_episode(self, d):
        media = d.get('Media', [{}])[0]
        part  = media.get('Part', [{}])[0]
        return {
            'id':             str(d.get('ratingKey', '')),
            'title':          d.get('title', ''),
            'summary':        d.get('summary', ''),
            'rating':         d.get('rating'),
            'thumb':          f"{self.url}{d['thumb']}?X-Plex-Token={self.token}" if d.get('thumb') else '',
            'added_at':       d.get('addedAt', 0),
            'duration':       d.get('duration', 0),
            'view_offset':    d.get('viewOffset', 0),
            'type':           'episode',
            'season_number':  d.get('parentIndex'),
            'episode_number': d.get('index'),
            'air_date':       d.get('originallyAvailableAt', ''),
            'media_parts':    [{'key': part.get('key', ''), 'container': part.get('container', 'mkv')}],
            'show_id':        str(d.get('grandparentRatingKey', '')),
        }

    def _normalize_season(self, d):
        return {
            'id':            str(d.get('ratingKey', '')),
            'title':         d.get('title', ''),
            'summary':       d.get('summary', ''),
            'season_number': d.get('index'),
            'year':          d.get('year'),
            'thumb':         f"{self.url}{d['thumb']}?X-Plex-Token={self.token}" if d.get('thumb') else '',
            'art':           f"{self.url}{d['art']}?X-Plex-Token={self.token}"   if d.get('art')   else '',
            'type':          'season',
        }

    def get_item(self, item_id):
        """Fetch a single item by ID and return normalized dict."""
        data  = self._get(f'/library/metadata/{item_id}')
        items = data.get('MediaContainer', {}).get('Metadata', [])
        if not items:
            return None
        d = items[0]
        t = d.get('type', '')
        if t == 'movie':   return self._normalize_movie(d)
        if t == 'show':    return self._normalize_show(d)
        if t == 'episode': return self._normalize_episode(d)
        return d

    def get_all_movies(self, lib_id, limit=None):
        params = {'type': 1}
        if limit:
            params['X-Plex-Container-Size'] = limit
        data = self._get(f'/library/sections/{lib_id}/all', params=params)
        items = data.get('MediaContainer', {}).get('Metadata', [])
        return [self._normalize_movie(d) for d in items]

    def get_all_shows(self, lib_id, limit=None):
        params = {'type': 2}
        if limit:
            params['X-Plex-Container-Size'] = limit
        data = self._get(f'/library/sections/{lib_id}/all', params=params)
        items = data.get('MediaContainer', {}).get('Metadata', [])
        return [self._normalize_show(d) for d in items]

    def get_recently_added(self, lib_id, limit=50):
        data  = self._get(f'/library/sections/{lib_id}/recentlyAdded',
                          params={'X-Plex-Container-Size': limit})
        items = data.get('MediaContainer', {}).get('Metadata', [])
        # recentlyAdded can return both movies and episodes
        result = []
        for d in items:
            t = d.get('type', '')
            if t == 'movie': result.append(self._normalize_movie(d))
            elif t == 'show': result.append(self._normalize_show(d))
        return result

    def get_unwatched_movies(self, lib_id, limit=None):
        params = {'type': 1, 'unwatched': 1}
        if limit:
            params['X-Plex-Container-Size'] = limit
        data  = self._get(f'/library/sections/{lib_id}/all', params=params)
        items = data.get('MediaContainer', {}).get('Metadata', [])
        return [self._normalize_movie(d) for d in items]

    def get_unwatched_shows(self, lib_id, limit=None):
        params = {'type': 2, 'unwatched': 1}
        if limit:
            params['X-Plex-Container-Size'] = limit
        data  = self._get(f'/library/sections/{lib_id}/all', params=params)
        items = data.get('MediaContainer', {}).get('Metadata', [])
        return [self._normalize_show(d) for d in items]

    def get_by_genre(self, lib_id, genre, media_type='movie', limit=200):
        plex_type = 1 if media_type == 'movie' else 2
        data  = self._get(f'/library/sections/{lib_id}/all',
                          params={'type': plex_type, 'genre': genre,
                                  'X-Plex-Container-Size': limit})
        items = data.get('MediaContainer', {}).get('Metadata', [])
        if media_type == 'movie':
            return [self._normalize_movie(d) for d in items]
        return [self._normalize_show(d) for d in items]

    def get_by_decade(self, lib_id, decade, media_type='movie', limit=200):
        plex_type = 1 if media_type == 'movie' else 2
        data  = self._get(f'/library/sections/{lib_id}/all',
                          params={'type': plex_type,
                                  'year>>': decade, 'year<<': decade + 9,
                                  'X-Plex-Container-Size': limit})
        items = data.get('MediaContainer', {}).get('Metadata', [])
        if media_type == 'movie':
            return [self._normalize_movie(d) for d in items]
        return [self._normalize_show(d) for d in items]

    def get_genres(self, lib_id, media_type='movie'):
        """Return sorted list of genre strings for a library section."""
        plex_type = 1 if media_type == 'movie' else 2
        try:
            # Sample up to 500 items to collect genres
            data  = self._get(f'/library/sections/{lib_id}/all',
                              params={'type': plex_type,
                                      'X-Plex-Container-Size': 500})
            items = data.get('MediaContainer', {}).get('Metadata', [])
            genres = set()
            for d in items:
                for g in d.get('Genre', []):
                    genres.add(g['tag'])
            return sorted(genres)
        except Exception as e:
            print(f"[PLEX] Error getting genres: {e}")
            return []

    def get_decades(self, lib_id, media_type='movie'):
        """Return sorted list of decade ints for a library section."""
        plex_type = 1 if media_type == 'movie' else 2
        try:
            data  = self._get(f'/library/sections/{lib_id}/all',
                              params={'type': plex_type,
                                      'X-Plex-Container-Size': 500})
            items = data.get('MediaContainer', {}).get('Metadata', [])
            decades = set()
            for d in items:
                year = d.get('year')
                if year:
                    decade = (year // 10) * 10
                    if decade >= 1920:
                        decades.add(decade)
            return sorted(decades, reverse=True)
        except Exception as e:
            print(f"[PLEX] Error getting decades: {e}")
            return []

    def get_collections(self, lib_id):
        """Return list of {'id', 'title'} dicts."""
        try:
            data  = self._get(f'/library/sections/{lib_id}/collections')
            items = data.get('MediaContainer', {}).get('Metadata', [])
            return [{'id': str(d['ratingKey']), 'title': d['title']} for d in items]
        except Exception as e:
            print(f"[PLEX] Error getting collections: {e}")
            return []

    def get_collection_items(self, collection_id, media_type='movie'):
        try:
            data  = self._get(f'/library/metadata/{collection_id}/children')
            items = data.get('MediaContainer', {}).get('Metadata', [])
            if media_type == 'movie':
                return [self._normalize_movie(d) for d in items]
            return [self._normalize_show(d) for d in items]
        except Exception as e:
            print(f"[PLEX] Error getting collection items: {e}")
            return []

    def get_on_deck(self, limit=50):
        """Return mixed list of normalized movie and episode dicts."""
        data  = self._get('/library/onDeck',
                          params={'X-Plex-Container-Size': limit * 2})
        items = data.get('MediaContainer', {}).get('Metadata', [])
        result = []
        for d in items:
            t = d.get('type', '')
            if   t == 'movie':   result.append(self._normalize_movie(d))
            elif t == 'episode': result.append(self._normalize_episode(d))
        return result

    def get_seasons(self, show_id):
        data  = self._get(f'/library/metadata/{show_id}/children')
        items = data.get('MediaContainer', {}).get('Metadata', [])
        return [self._normalize_season(d) for d in items if d.get('type') == 'season']

    def get_episodes(self, season_id):
        data  = self._get(f'/library/metadata/{season_id}/children')
        items = data.get('MediaContainer', {}).get('Metadata', [])
        return [self._normalize_episode(d) for d in items if d.get('type') == 'episode']

    def search(self, lib_id, query, media_type='movie', limit=20):
        plex_type = 1 if media_type == 'movie' else 2
        data  = self._get(f'/library/sections/{lib_id}/all',
                          params={'type': plex_type, 'title': query,
                                  'X-Plex-Container-Size': limit})
        items = data.get('MediaContainer', {}).get('Metadata', [])
        if media_type == 'movie':
            return [self._normalize_movie(d) for d in items]
        return [self._normalize_show(d) for d in items]

    def get_stream_url(self, item):
        """Return a direct stream URL for a normalized item."""
        parts = item.get('media_parts', [])
        if not parts or not parts[0].get('key'):
            return None
        return f"{self.url}{parts[0]['key']}?X-Plex-Token={self.token}"

    def get_show_for_episode(self, episode):
        """Fetch and return the parent show for a normalized episode dict."""
        show_id = episode.get('show_id')
        if not show_id:
            return None
        return self.get_item(show_id)


# ─────────────────────────────────────────────────────────────────────────────
# EmbyJellyfinClient — raw HTTP interface for Emby and Jellyfin
# Both servers share almost identical APIs; the only differences are the
# auth header name and a few minor endpoint paths.
# ─────────────────────────────────────────────────────────────────────────────

class EmbyJellyfinClient:
    """
    HTTP client for Emby and Jellyfin media servers.
    Returns normalized dicts matching PlexClient's output format exactly.
    """

    def __init__(self, url, api_key, user_id, flavour='jellyfin'):
        self.url         = url.rstrip('/')
        self.api_key     = api_key
        self.user_id     = user_id
        self.flavour     = flavour   # 'emby' or 'jellyfin'
        self.server_name = ''
        self.machine_id  = ''
        self._session    = requests.Session()
        # Both servers accept api_key as query param — simplest universal approach
        self._session.headers.update({
            'Accept': 'application/json',
        })

    # ── Connection ──────────────────────────────────────────────────────────

    def connect(self):
        try:
            r    = self._get('/System/Info')
            self.server_name = r.get('ServerName', '')
            self.machine_id  = r.get('Id', '')
            return True
        except Exception as e:
            print(f"[{self.flavour.upper()}] Connection failed: {e}")
            return False

    def _get(self, path, params=None):
        p = {'api_key': self.api_key}
        if params:
            p.update(params)
        r = self._session.get(self.url + path, params=p, timeout=30)
        r.raise_for_status()
        return r.json()

    # ── User discovery (for setup UI) ───────────────────────────────────────

    def get_users(self):
        """Return list of {'id', 'name'} for all users — used by setup UI."""
        try:
            users = self._get('/Users')
            return [{'id': u['Id'], 'name': u['Name']} for u in users]
        except Exception as e:
            print(f"[{self.flavour.upper()}] Error fetching users: {e}")
            return []

    # ── Libraries ───────────────────────────────────────────────────────────

    def get_libraries(self):
        data  = self._get(f'/Users/{self.user_id}/Views')
        items = data.get('Items', [])
        libs  = []
        for item in items:
            col_type = item.get('CollectionType', '')
            if col_type == 'movies':
                lib_type = 'movie'
            elif col_type == 'tvshows':
                lib_type = 'show'
            else:
                continue   # skip music, photos, etc.
            libs.append({'id': item['Id'], 'title': item['Name'], 'type': lib_type})
        return libs

    def total_view_size(self, lib_id, media_type='movie'):
        try:
            include_type = 'Movie' if media_type == 'movie' else 'Series'
            r = self._get(f'/Users/{self.user_id}/Items', {
                'ParentId': lib_id, 'IncludeItemTypes': include_type,
                'Recursive': 'true', 'Limit': 0
            })
            return r.get('TotalRecordCount', 0)
        except Exception:
            return 0

    # ── Normalization ────────────────────────────────────────────────────────

    def _img(self, item_id, tag, img_type='Primary'):
        """Build an absolute image URL."""
        if not tag:
            return ''
        return f"{self.url}/Items/{item_id}/Images/{img_type}?api_key={self.api_key}"

    def _normalize_movie(self, d):
        item_id = d.get('Id', '')
        genres  = d.get('Genres', [])
        stream_path = f"/Videos/{item_id}/stream?api_key={self.api_key}&static=true"
        return {
            'id':             item_id,
            'title':          d.get('Name', ''),
            'year':           d.get('ProductionYear'),
            'summary':        d.get('Overview', ''),
            'rating':         d.get('CommunityRating'),
            'content_rating': d.get('OfficialRating', ''),
            'thumb':          self._img(item_id, d.get('ImageTags', {}).get('Primary')),
            'art':            self._img(item_id, d.get('BackdropImageTags', [''])[0] if d.get('BackdropImageTags') else '', 'Backdrop'),
            'genres':         genres,
            'added_at':       0,
            'duration':       d.get('RunTimeTicks', 0) // 10000 if d.get('RunTimeTicks') else 0,
            'view_offset':    d.get('UserData', {}).get('PlaybackPositionTicks', 0) // 10000,
            'type':           'movie',
            'media_parts':    [{'key': stream_path, 'container': 'mkv'}],
            'directors':      [p['Name'] for p in d.get('People', []) if p.get('Type') == 'Director'],
            'roles':          [p['Name'] for p in d.get('People', []) if p.get('Type') == 'Actor'][:10],
            'original_title': d.get('OriginalTitle', d.get('Name', '')),
        }

    def _normalize_show(self, d):
        item_id = d.get('Id', '')
        return {
            'id':        item_id,
            'title':     d.get('Name', ''),
            'year':      d.get('ProductionYear'),
            'summary':   d.get('Overview', ''),
            'rating':    d.get('CommunityRating'),
            'thumb':     self._img(item_id, d.get('ImageTags', {}).get('Primary')),
            'art':       self._img(item_id, d.get('BackdropImageTags', [''])[0] if d.get('BackdropImageTags') else '', 'Backdrop'),
            'genres':    d.get('Genres', []),
            'added_at':  0,
            'duration':  0,
            'type':      'show',
            'directors': [],
            'roles':     [p['Name'] for p in d.get('People', []) if p.get('Type') == 'Actor'][:10],
        }

    def _normalize_episode(self, d):
        item_id     = d.get('Id', '')
        stream_path = f"/Videos/{item_id}/stream?api_key={self.api_key}&static=true"
        return {
            'id':             item_id,
            'title':          d.get('Name', ''),
            'summary':        d.get('Overview', ''),
            'rating':         d.get('CommunityRating'),
            'thumb':          self._img(item_id, d.get('ImageTags', {}).get('Primary')),
            'added_at':       0,
            'duration':       d.get('RunTimeTicks', 0) // 10000 if d.get('RunTimeTicks') else 0,
            'view_offset':    d.get('UserData', {}).get('PlaybackPositionTicks', 0) // 10000,
            'type':           'episode',
            'season_number':  d.get('ParentIndexNumber'),
            'episode_number': d.get('IndexNumber'),
            'air_date':       d.get('PremiereDate', '')[:10] if d.get('PremiereDate') else '',
            'media_parts':    [{'key': stream_path, 'container': 'mkv'}],
            'show_id':        d.get('SeriesId', ''),
        }

    def _normalize_season(self, d):
        item_id = d.get('Id', '')
        return {
            'id':            item_id,
            'title':         d.get('Name', ''),
            'summary':       d.get('Overview', ''),
            'season_number': d.get('IndexNumber'),
            'year':          d.get('ProductionYear'),
            'thumb':         self._img(item_id, d.get('ImageTags', {}).get('Primary')),
            'art':           '',
            'type':          'season',
        }

    # ── Items ────────────────────────────────────────────────────────────────

    def _items(self, params):
        params.setdefault('UserId', self.user_id)
        params.setdefault('Recursive', 'true')
        r = self._get(f'/Users/{self.user_id}/Items', params)
        return r.get('Items', [])

    def get_item(self, item_id):
        try:
            d = self._get(f'/Users/{self.user_id}/Items/{item_id}')
            t = d.get('Type', '')
            if t == 'Movie':   return self._normalize_movie(d)
            if t == 'Series':  return self._normalize_show(d)
            if t == 'Episode': return self._normalize_episode(d)
            return d
        except Exception as e:
            print(f"[{self.flavour.upper()}] get_item error: {e}")
            return None

    def get_all_movies(self, lib_id, limit=None):
        print(f"[{self.flavour.upper()}] get_all_movies: {self.url}/Users/{self.user_id}/Items?ParentId={lib_id}")
        p = {'ParentId': lib_id, 'IncludeItemTypes': 'Movie',
             'Fields': 'Genres,People,Overview,BackdropImageTags'}
        if limit:
            p['Limit'] = limit
        return [self._normalize_movie(d) for d in self._items(p)]

    def get_all_shows(self, lib_id, limit=None):
        p = {'ParentId': lib_id, 'IncludeItemTypes': 'Series',
             'Fields': 'Genres,People,Overview,BackdropImageTags'}
        if limit:
            p['Limit'] = limit
        return [self._normalize_show(d) for d in self._items(p)]

    def get_recently_added(self, lib_id, limit=50):
        p = {'ParentId': lib_id, 'Limit': limit,
             'SortBy': 'DateCreated', 'SortOrder': 'Descending',
             'Fields': 'Genres,Overview,BackdropImageTags'}
        items = self._items(p)
        result = []
        for d in items:
            t = d.get('Type', '')
            if t == 'Movie':  result.append(self._normalize_movie(d))
            elif t == 'Series': result.append(self._normalize_show(d))
        return result

    def get_unwatched_movies(self, lib_id, limit=None):
        p = {'ParentId': lib_id, 'IncludeItemTypes': 'Movie',
             'IsPlayed': 'false', 'Fields': 'Genres,Overview,BackdropImageTags'}
        if limit:
            p['Limit'] = limit
        return [self._normalize_movie(d) for d in self._items(p)]

    def get_unwatched_shows(self, lib_id, limit=None):
        p = {'ParentId': lib_id, 'IncludeItemTypes': 'Series',
             'IsPlayed': 'false', 'Fields': 'Genres,Overview,BackdropImageTags'}
        if limit:
            p['Limit'] = limit
        return [self._normalize_show(d) for d in self._items(p)]

    def get_by_genre(self, lib_id, genre, media_type='movie', limit=200):
        include = 'Movie' if media_type == 'movie' else 'Series'
        p = {'ParentId': lib_id, 'IncludeItemTypes': include,
             'Genres': genre, 'Limit': limit,
             'Fields': 'Genres,Overview,BackdropImageTags'}
        items = self._items(p)
        if media_type == 'movie':
            return [self._normalize_movie(d) for d in items]
        return [self._normalize_show(d) for d in items]

    def get_by_decade(self, lib_id, decade, media_type='movie', limit=200):
        include = 'Movie' if media_type == 'movie' else 'Series'
        p = {'ParentId': lib_id, 'IncludeItemTypes': include,
             'Years': ','.join(str(y) for y in range(decade, decade + 10)),
             'Limit': limit, 'Fields': 'Genres,Overview,BackdropImageTags'}
        items = self._items(p)
        if media_type == 'movie':
            return [self._normalize_movie(d) for d in items]
        return [self._normalize_show(d) for d in items]

    def get_genres(self, lib_id, media_type='movie'):
        include = 'Movie' if media_type == 'movie' else 'Series'
        try:
            r = self._get('/Genres', {'ParentId': lib_id,
                                      'IncludeItemTypes': include,
                                      'UserId': self.user_id})
            return sorted(item['Name'] for item in r.get('Items', []))
        except Exception as e:
            print(f"[{self.flavour.upper()}] Error getting genres: {e}")
            return []

    def get_decades(self, lib_id, media_type='movie'):
        include = 'Movie' if media_type == 'movie' else 'Series'
        try:
            items   = self._items({'ParentId': lib_id, 'IncludeItemTypes': include,
                                   'Fields': '', 'Limit': 2000})
            decades = set()
            for d in items:
                year = d.get('ProductionYear')
                if year:
                    decade = (year // 10) * 10
                    if decade >= 1920:
                        decades.add(decade)
            return sorted(decades, reverse=True)
        except Exception as e:
            print(f"[{self.flavour.upper()}] Error getting decades: {e}")
            return []

    def get_collections(self, lib_id):
        try:
            items = self._items({'ParentId': lib_id, 'IncludeItemTypes': 'BoxSet',
                                 'Fields': ''})
            return [{'id': d['Id'], 'title': d['Name']} for d in items]
        except Exception as e:
            print(f"[{self.flavour.upper()}] Error getting collections: {e}")
            return []

    def get_collection_items(self, collection_id, media_type='movie'):
        try:
            items = self._items({'ParentId': collection_id,
                                 'Fields': 'Genres,Overview,BackdropImageTags'})
            if media_type == 'movie':
                return [self._normalize_movie(d) for d in items if d.get('Type') == 'Movie']
            return [self._normalize_show(d) for d in items if d.get('Type') == 'Series']
        except Exception as e:
            print(f"[{self.flavour.upper()}] Error getting collection items: {e}")
            return []

    def get_on_deck(self, limit=50):
        try:
            r     = self._get(f'/Users/{self.user_id}/Items/Resume',
                              {'Limit': limit * 2,
                               'Fields': 'Genres,Overview,BackdropImageTags,UserData'})
            items = r.get('Items', [])
            result = []
            for d in items:
                t = d.get('Type', '')
                if   t == 'Movie':   result.append(self._normalize_movie(d))
                elif t == 'Episode': result.append(self._normalize_episode(d))
            return result
        except Exception as e:
            print(f"[{self.flavour.upper()}] Error getting on deck: {e}")
            return []

    def get_seasons(self, show_id):
        try:
            r = self._get(f'/Shows/{show_id}/Seasons',
                          {'UserId': self.user_id, 'Fields': 'Overview'})
            return [self._normalize_season(d) for d in r.get('Items', [])]
        except Exception as e:
            print(f"[{self.flavour.upper()}] Error getting seasons: {e}")
            return []

    def get_episodes(self, season_id):
        try:
            # season_id here is the season's item ID
            r = self._get(f'/Users/{self.user_id}/Items',
                          {'ParentId': season_id, 'Fields': 'Overview,UserData'})
            return [self._normalize_episode(d) for d in r.get('Items', [])
                    if d.get('Type') == 'Episode']
        except Exception as e:
            print(f"[{self.flavour.upper()}] Error getting episodes: {e}")
            return []

    def search(self, lib_id, query, media_type='movie', limit=20):
        include = 'Movie' if media_type == 'movie' else 'Series'
        items   = self._items({'ParentId': lib_id, 'SearchTerm': query,
                               'IncludeItemTypes': include, 'Limit': limit,
                               'Fields': 'Genres,Overview'})
        if media_type == 'movie':
            return [self._normalize_movie(d) for d in items]
        return [self._normalize_show(d) for d in items]

    def get_stream_url(self, item):
        parts = item.get('media_parts', [])
        if not parts or not parts[0].get('key'):
            return None
        return self.url + parts[0]['key']

    def get_show_for_episode(self, episode):
        show_id = episode.get('show_id')
        if not show_id:
            return None
        return self.get_item(show_id)


# ─────────────────────────────────────────────────────────────────────────────
# Global media server client — set by connect_server() on startup
# ─────────────────────────────────────────────────────────────────────────────
media_client = None
plex         = None   # alias kept for any missed references during transition
cache_queue = Queue()
cache_warming_active = False
last_library_scan = 0
known_items = set()  # Track known movie/show IDs

def scan_for_new_content():
    """Periodically scan for new content and cache TMDb metadata."""
    global last_library_scan, known_items

    if not media_client or not TMDB_API_KEY:
        return

    current_time = time.time()
    if current_time - last_library_scan < 300:
        return
    last_library_scan = current_time

    try:
        new_items_found = 0
        for section in get_cached_sections():
            if section['type'] == 'movie':
                for item in media_client.get_recently_added(section['id'], 50):
                    if item['type'] != 'movie':
                        continue
                    item_id = f"movie_{item['id']}"
                    if item_id not in known_items:
                        known_items.add(item_id)
                        if item_id not in session_cache.get('movies', {}):
                            cache_queue.put(('movie', item))
                            new_items_found += 1
            elif section['type'] == 'show':
                for item in media_client.get_recently_added(section['id'], 50):
                    if item['type'] != 'show':
                        continue
                    item_id = f"show_{item['id']}"
                    if item_id not in known_items:
                        known_items.add(item_id)
                        if f"series_{item['id']}" not in session_cache.get('series', {}):
                            cache_queue.put(('series', item))
                            new_items_found += 1

        if new_items_found > 0:
            print(f"[AUTO-CACHE] Queued {new_items_found} new items for caching")
    except Exception as e:
        print(f"[AUTO-CACHE] Error scanning for new content: {e}")

def initialize_known_items():
    """Build initial set of known items from the media server."""
    global known_items
    if not media_client:
        return
    try:
        for section in get_cached_sections():
            if section['type'] == 'movie':
                for item in media_client.get_all_movies(section['id']):
                    known_items.add(f"movie_{item['id']}")
            elif section['type'] == 'show':
                for item in media_client.get_all_shows(section['id']):
                    known_items.add(f"show_{item['id']}")
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

            item_type, norm_item = item

            # Validate item — normalized dicts always have 'id' and 'title'
            if not norm_item or not isinstance(norm_item, dict) or 'id' not in norm_item:
                print(f"[CACHE] Invalid item in queue: {item}")
                cache_queue.task_done()
                continue

            cache_key      = f"{item_type}_{norm_item['id']}"
            cache_category = 'movies' if item_type == 'movie' else 'series'

            # Skip if already cached
            if cache_key in session_cache.get(cache_category, {}):
                cache_queue.task_done()
                continue

            # Fetch TMDb data
            try:
                if item_type == 'movie':
                    tmdb_data = enhance_movie_with_tmdb(norm_item)
                elif item_type == 'series':
                    tmdb_data = enhance_series_with_tmdb(norm_item)
                else:
                    items_failed += 1
                    cache_queue.task_done()
                    continue

                if tmdb_data:
                    if cache_category not in session_cache:
                        session_cache[cache_category] = {}
                    session_cache[cache_category][cache_key] = tmdb_data
                    items_processed += 1
                    title = norm_item.get('title', cache_key)
                    print(f"[CACHE] ✓ Cached {item_type}: {title}")

                    if items_processed % 10 == 0:
                        remaining = cache_queue.qsize()
                        movies    = len(session_cache.get('movies', {}))
                        series    = len(session_cache.get('series', {}))
                        print(f"[CACHE] Progress: {items_processed} cached, {items_failed} failed, {remaining} in queue (Movies: {movies}, Shows: {series})")
                else:
                    items_failed += 1
                    print(f"[CACHE] ✗ No TMDb data for {item_type}: {norm_item.get('title', cache_key)}")
            except Exception as e:
                items_failed += 1
                print(f"[CACHE] ✗ Error caching {norm_item.get('title', 'item')}: {e}")
            
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
    """Queue items from library for background TMDb caching."""
    if not TMDB_API_KEY:
        print("[CACHE] TMDb API key not set, skipping cache warming")
        return
    if not media_client:
        return

    cache_type = 'movie' if section_type == 'movie' else 'series'
    count = 0

    try:
        for section in get_cached_sections():
            if section['type'] == section_type or (section_type != 'movie' and section['type'] == 'show'):
                if section_type == 'movie':
                    items = media_client.get_all_movies(section['id'])
                else:
                    items = media_client.get_all_shows(section['id'])

                print(f"[CACHE] Found {len(items)} items in section '{section['title']}'")

                for item in items:
                    if limit and count >= limit:
                        break
                    cache_key = f"{cache_type}_{item['id']}"
                    cache_bucket = 'movies' if cache_type == 'movie' else 'series'
                    if cache_key not in session_cache.get(cache_bucket, {}):
                        cache_queue.put((cache_type, item))
                        count += 1

                if limit and count >= limit:
                    break

        print(f"[CACHE] Queued {count} items for background caching")
    except Exception as e:
        print(f"[CACHE] Error warming cache: {e}")

# Configuration - Update these with your settings
PLEX_URL    = os.getenv('PLEX_URL', '')
PLEX_TOKEN  = os.getenv('PLEX_TOKEN', '')
SERVER_TYPE = os.getenv('SERVER_TYPE', 'plex')   # 'plex', 'emby', 'jellyfin'
EMBY_URL     = os.getenv('EMBY_URL', '')
EMBY_API_KEY = os.getenv('EMBY_API_KEY', '')
EMBY_USER_ID = os.getenv('EMBY_USER_ID', '')
BRIDGE_USERNAME = os.getenv('BRIDGE_USERNAME', 'admin')
BRIDGE_PASSWORD = os.getenv('BRIDGE_PASSWORD', 'admin')
BRIDGE_HOST = os.getenv('BRIDGE_HOST', '0.0.0.0')
BRIDGE_PORT = int(os.getenv('BRIDGE_PORT', '9999'))
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
CACHE_FILE            = os.path.join(DATA_DIR, 'tmdb_cache.json')
STATS_FILE            = os.path.join(DATA_DIR, 'stats.json')
CATEGORY_FILTERS_FILE = os.path.join(DATA_DIR, 'category_filters.json')

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
    """Background task to auto-match unmatched content with TMDb."""
    global auto_matching_running, last_auto_match_time

    if not TMDB_API_KEY or not media_client:
        return

    auto_matching_running = True
    matched_count = 0

    try:
        print("[AUTO-MATCH] Starting auto-match scan...")

        for section in get_cached_sections():
            if section['type'] == 'movie':
                for item in media_client.get_all_movies(section['id']):
                    cache_key = f"movie_{item['id']}"
                    if cache_key not in session_cache['movies']:
                        tmdb_data = enhance_movie_with_tmdb(item)
                        if tmdb_data:
                            session_cache['movies'][cache_key] = tmdb_data
                            matched_count += 1
                            print(f"[AUTO-MATCH] Matched movie: {item.get('title', item['id'])}")
            elif section['type'] == 'show':
                for item in media_client.get_all_shows(section['id']):
                    cache_key = f"series_{item['id']}"
                    if cache_key not in session_cache['series']:
                        tmdb_data = enhance_series_with_tmdb(item)
                        if tmdb_data:
                            session_cache['series'][cache_key] = tmdb_data
                            matched_count += 1
                            print(f"[AUTO-MATCH] Matched show: {item.get('title', item['id'])}")

        print(f"[AUTO-MATCH] Completed! Matched {matched_count} items")
        last_auto_match_time = time.time()

        if matched_count > 0:
            save_cache_to_disk()

    except Exception as e:
        print(f"[AUTO-MATCH] Error: {e}")
    finally:
        auto_matching_running = False


def scan_for_new_plex_content():
    """Scan for new content and pre-cache TMDb data."""
    global known_item_ids, last_scan_time

    if not media_client or not TMDB_API_KEY:
        return

    try:
        new_items = 0
        for section in get_cached_sections():
            if section['type'] == 'movie':
                for item in media_client.get_all_movies(section['id']):
                    item_id = f"movie_{item['id']}"
                    if item_id not in known_item_ids:
                        known_item_ids.add(item_id)
                        cache_key = f"movie_{item['id']}"
                        if cache_key not in session_cache['movies']:
                            tmdb_data = enhance_movie_with_tmdb(item)
                            if tmdb_data:
                                session_cache['movies'][cache_key] = tmdb_data
                                new_items += 1
                                print(f"[NEW] Cached new movie: {item.get('title', item['id'])}")
            elif section['type'] == 'show':
                for item in media_client.get_all_shows(section['id']):
                    item_id = f"show_{item['id']}"
                    if item_id not in known_item_ids:
                        known_item_ids.add(item_id)
                        cache_key = f"series_{item['id']}"
                        if cache_key not in session_cache['series']:
                            tmdb_data = enhance_series_with_tmdb(item)
                            if tmdb_data:
                                session_cache['series'][cache_key] = tmdb_data
                                new_items += 1
                                print(f"[NEW] Cached new show: {item.get('title', item['id'])}")

        if new_items > 0:
            print(f"[SCAN] Found and cached {new_items} new items")
        last_scan_time = time.time()

    except Exception as e:
        print(f"[SCAN] Error scanning for new content: {e}")


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
    """Get library sections with caching to reduce API calls."""
    current_time = time.time()
    if session_cache['sections'] and (current_time - session_cache['sections_time']) < 300:
        return session_cache['sections']
    if media_client:
        session_cache['sections']      = media_client.get_libraries()
        session_cache['sections_time'] = current_time
    return session_cache.get('sections') or []

# Track known items to detect new content
known_item_ids = set()
last_scan_time = 0

def background_auto_matcher():
    """Background thread that runs TMDb auto-matching on startup then every 30 minutes."""
    print("[AUTO-MATCH] Background auto-matcher started")
    auto_match_content()
    while True:
        time.sleep(1800)
        if not auto_matching_running:
            print("[AUTO-MATCH] Running scheduled auto-match...")
            auto_match_content()


def background_scanner():
    """Background thread that scans every 15 minutes."""
    global last_scan_time
    print("[SCAN] Background scanner started - checking every 15 minutes")
    last_scan_time = time.time()
    while True:
        time.sleep(900)
        print("[SCAN] Running 15-minute scan for new content...")
        scan_for_new_plex_content()


def clear_metadata_cache():
    """Clear the in-memory TMDb cache."""
    session_cache['movies'] = {}
    session_cache['series'] = {}
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

def enhance_movie_with_tmdb(item):
    """Enhance a normalized movie dict with TMDb metadata."""
    if not TMDB_API_KEY:
        return {}
    try:
        tmdb_data = fetch_tmdb_data(item.get('title', ''), item.get('year'), 'movie')
        if tmdb_data:
            return {
                'tmdb_id':      tmdb_data.get('id'),
                'imdb_id':      tmdb_data.get('imdb_id', ''),
                'overview':     tmdb_data.get('overview', ''),
                'tagline':      tmdb_data.get('tagline', ''),
                'popularity':   tmdb_data.get('popularity', 0),
                'vote_average': tmdb_data.get('vote_average', 0),
                'vote_count':   tmdb_data.get('vote_count', 0),
                'backdrop_path': f"https://image.tmdb.org/t/p/original{tmdb_data['backdrop_path']}" if tmdb_data.get('backdrop_path') else '',
                'poster_path':   f"https://image.tmdb.org/t/p/original{tmdb_data['poster_path']}"   if tmdb_data.get('poster_path')   else '',
                'genres':   [g['name'] for g in tmdb_data.get('genres', [])],
                'keywords': [k['name'] for k in tmdb_data.get('keywords', {}).get('keywords', [])],
                'cast':     [{'name': c['name'], 'character': c['character']} for c in tmdb_data.get('credits', {}).get('cast', [])[:10]],
                'director': next((c['name'] for c in tmdb_data.get('credits', {}).get('crew', []) if c['job'] == 'Director'), ''),
                'trailer':  next((f"https://www.youtube.com/watch?v={v['key']}" for v in tmdb_data.get('videos', {}).get('results', []) if v['site'] == 'YouTube' and v['type'] == 'Trailer'), ''),
            }
    except Exception as e:
        print(f"Error enhancing movie with TMDb: {e}")
    return {}


def enhance_series_with_tmdb(item):
    """Enhance a normalized show dict with TMDb metadata."""
    if not TMDB_API_KEY:
        return {}
    try:
        tmdb_data = fetch_tmdb_data(item.get('title', ''), item.get('year'), 'tv')
        if tmdb_data:
            return {
                'tmdb_id':      tmdb_data.get('id'),
                'overview':     tmdb_data.get('overview', ''),
                'popularity':   tmdb_data.get('popularity', 0),
                'vote_average': tmdb_data.get('vote_average', 0),
                'vote_count':   tmdb_data.get('vote_count', 0),
                'backdrop_path': f"https://image.tmdb.org/t/p/original{tmdb_data['backdrop_path']}" if tmdb_data.get('backdrop_path') else '',
                'poster_path':   f"https://image.tmdb.org/t/p/original{tmdb_data['poster_path']}"   if tmdb_data.get('poster_path')   else '',
                'genres':             [g['name'] for g in tmdb_data.get('genres', [])],
                'keywords':           [k['name'] for k in tmdb_data.get('keywords', {}).get('results', [])] if tmdb_data.get('keywords') else [],
                'cast':               [{'name': c['name'], 'character': c['character']} for c in tmdb_data.get('credits', {}).get('cast', [])[:10]] if tmdb_data.get('credits') else [],
                'created_by':         [c['name'] for c in tmdb_data.get('created_by', [])] if tmdb_data.get('created_by') else [],
                'networks':           [n['name'] for n in tmdb_data.get('networks', [])]    if tmdb_data.get('networks')    else [],
                'number_of_seasons':  tmdb_data.get('number_of_seasons', 0),
                'number_of_episodes': tmdb_data.get('number_of_episodes', 0),
                'status':  tmdb_data.get('status', ''),
                'trailer': next((f"https://www.youtube.com/watch?v={v['key']}" for v in tmdb_data.get('videos', {}).get('results', []) if v.get('site') == 'YouTube' and v.get('type') == 'Trailer'), '') if tmdb_data.get('videos') else '',
            }
    except Exception as e:
        print(f"[ERROR] Error enhancing series '{item.get('title', 'Unknown')}' with TMDb: {e}")
    return {}

def load_config():
    """Load configuration from file."""
    global PLEX_URL, PLEX_TOKEN, SERVER_TYPE, EMBY_URL, EMBY_API_KEY, EMBY_USER_ID
    global BRIDGE_USERNAME, BRIDGE_PASSWORD, ADMIN_PASSWORD, SHOW_DUMMY_CHANNEL, TMDB_API_KEY, custom_categories

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)

            SERVER_TYPE = config.get('server_type', SERVER_TYPE)

            PLEX_URL   = config.get('plex_url', PLEX_URL)
            enc_token  = config.get('plex_token', PLEX_TOKEN)
            PLEX_TOKEN = decrypt_value(enc_token) if enc_token else PLEX_TOKEN

            EMBY_URL     = config.get('emby_url', EMBY_URL)
            enc_emby_key = config.get('emby_api_key', EMBY_API_KEY)
            EMBY_API_KEY = decrypt_value(enc_emby_key) if enc_emby_key else EMBY_API_KEY
            EMBY_USER_ID = config.get('emby_user_id', EMBY_USER_ID)

            BRIDGE_USERNAME    = config.get('bridge_username', BRIDGE_USERNAME)
            BRIDGE_PASSWORD    = config.get('bridge_password', BRIDGE_PASSWORD)
            ADMIN_PASSWORD     = config.get('admin_password', ADMIN_PASSWORD)
            SHOW_DUMMY_CHANNEL = config.get('show_dummy_channel', SHOW_DUMMY_CHANNEL)

            enc_tmdb    = config.get('tmdb_api_key', TMDB_API_KEY)
            TMDB_API_KEY = decrypt_value(enc_tmdb) if enc_tmdb else TMDB_API_KEY

            print("✓ Configuration loaded from file")
        except Exception as e:
            print(f"✗ Error loading config: {e}")

    if os.path.exists(CATEGORIES_FILE):
        try:
            with open(CATEGORIES_FILE, 'r') as f:
                custom_categories = json.load(f)
            print(f"✓ Loaded {len(custom_categories.get('movies', []))} movie categories and {len(custom_categories.get('series', []))} series categories")
        except Exception as e:
            print(f"✗ Error loading categories: {e}")

def save_config():
    """Save configuration to file."""
    config = {
        'server_type':      SERVER_TYPE,
        'plex_url':         PLEX_URL,
        'plex_token':       encrypt_value(PLEX_TOKEN),
        'emby_url':         EMBY_URL,
        'emby_api_key':     encrypt_value(EMBY_API_KEY),
        'emby_user_id':     EMBY_USER_ID,
        'bridge_username':  BRIDGE_USERNAME,
        'bridge_password':  BRIDGE_PASSWORD,
        'admin_password':   hash_password(ADMIN_PASSWORD),
        'show_dummy_channel': SHOW_DUMMY_CHANNEL,
        'tmdb_api_key':     encrypt_value(TMDB_API_KEY),
    }
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
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

# ─────────────────────────────────────────────────────────────────────────────
# Special category ID constants — defined here so both the category filter
# system and the On Deck functions can reference them without ordering issues
# ─────────────────────────────────────────────────────────────────────────────

ON_DECK_MOVIE_CAT_ID    = "9000"
ON_DECK_SERIES_CAT_ID   = "9001"
UNWATCHED_MOVIE_CAT_ID  = "9002"
UNWATCHED_SERIES_CAT_ID = "9003"
ON_DECK_LIMIT           = int(os.getenv('ON_DECK_LIMIT', '50'))

# ─────────────────────────────────────────────────────────────────────────────
# Category filters — opt-in per-category visibility control
# ─────────────────────────────────────────────────────────────────────────────

# In-memory filter state. Structure:
#   { 'movies': { 'special': {'9000': True/False, ...},
#                 'smart':   {'10001': True/False, ...} },
#     'series': { 'special': {'9001': True/False, ...},
#                 'smart':   {'20001': True/False, ...} } }
category_filters = {
    'movies': {'special': {}, 'smart': {}},
    'series': {'special': {}, 'smart': {}}
}

# IDs for the special (non-smart) categories
SPECIAL_CATEGORY_DEFS = {
    'movies': [
        {'id': ON_DECK_MOVIE_CAT_ID,   'name': '▶ Continue Watching'},
        {'id': UNWATCHED_MOVIE_CAT_ID, 'name': '🎬 Unwatched Movies'},
    ],
    'series': [
        {'id': ON_DECK_SERIES_CAT_ID,   'name': '▶ Continue Watching'},
        {'id': UNWATCHED_SERIES_CAT_ID, 'name': '📺 Unwatched Shows'},
    ],
}


def load_category_filters():
    """Load saved category filter state from disk."""
    global category_filters
    try:
        if os.path.exists(CATEGORY_FILTERS_FILE):
            with open(CATEGORY_FILTERS_FILE, 'r') as f:
                saved = json.load(f)
            # Merge saved state — only update keys that exist in saved
            for media_type in ('movies', 'series'):
                for bucket in ('special', 'smart'):
                    if media_type in saved and bucket in saved[media_type]:
                        category_filters[media_type][bucket].update(
                            saved[media_type][bucket]
                        )
            print(f"✓ Category filters loaded from disk")
        else:
            print("✓ No category filters file — all categories hidden by default (opt-in)")
    except Exception as e:
        print(f"✗ Error loading category filters: {e}")


def save_category_filters():
    """Persist current category filter state to disk."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CATEGORY_FILTERS_FILE, 'w') as f:
            json.dump(category_filters, f, indent=2)
        print("✓ Category filters saved")
        return True
    except Exception as e:
        print(f"✗ Error saving category filters: {e}")
        return False


def is_category_enabled(cat_id, media_type):
    """
    Return True if the given category ID is enabled for the given media type.
    Opt-in: any ID not explicitly set to True is treated as hidden.
    """
    cat_id = str(cat_id)
    # Check special bucket first, then smart
    special = category_filters.get(media_type, {}).get('special', {})
    smart   = category_filters.get(media_type, {}).get('smart', {})
    if cat_id in special:
        return special[cat_id]
    if cat_id in smart:
        return smart[cat_id]
    return False  # opt-in default — hidden until explicitly enabled


def get_full_category_state():
    """
    Return the complete category list for both movies and series, each entry
    annotated with its current enabled state.  Used by the UI.
    """
    result = {'movies': {'special': [], 'smart': []},
              'series': {'special': [], 'smart': []}}

    for media_type, fn in (('movies', get_smart_categories_for_movies),
                           ('series', get_smart_categories_for_series)):
        # Special categories
        for cat in SPECIAL_CATEGORY_DEFS[media_type]:
            result[media_type]['special'].append({
                'id':      cat['id'],
                'name':    cat['name'],
                'enabled': is_category_enabled(cat['id'], media_type),
            })

        # Smart categories (genres, decades, collections, recently added)
        try:
            for cat in fn():
                result[media_type]['smart'].append({
                    'id':      cat['id'],
                    'name':    cat['name'],
                    'enabled': is_category_enabled(cat['id'], media_type),
                })
        except Exception as e:
            print(f"[CATEGORIES] Error building {media_type} smart list: {e}")

    return result

def connect_server():
    """Connect to the configured media server (Plex, Emby, or Jellyfin)."""
    global plex, media_client

    # Always clear the sections cache so stale IDs from a previous server
    # don't get used with the new connection
    session_cache['sections']      = None
    session_cache['sections_time'] = 0

    if SERVER_TYPE == 'plex':
        if not PLEX_URL or not PLEX_TOKEN:
            return False
        try:
            client = PlexClient(PLEX_URL, PLEX_TOKEN)
            if client.connect():
                media_client = client
                plex         = client
                print(f"✓ Connected to Plex Server: {client.server_name}")
                return True
            media_client = plex = None
            return False
        except Exception as e:
            print(f"✗ Failed to connect to Plex: {e}")
            media_client = plex = None
            return False

    elif SERVER_TYPE in ('emby', 'jellyfin'):
        if not EMBY_URL or not EMBY_API_KEY or not EMBY_USER_ID:
            return False
        try:
            client = EmbyJellyfinClient(EMBY_URL, EMBY_API_KEY, EMBY_USER_ID, SERVER_TYPE)
            if client.connect():
                media_client = client
                plex         = client   # alias
                print(f"✓ Connected to {SERVER_TYPE.title()} Server: {client.server_name}")
                return True
            media_client = plex = None
            return False
        except Exception as e:
            print(f"✗ Failed to connect to {SERVER_TYPE.title()}: {e}")
            media_client = plex = None
            return False

    print(f"✗ Unknown server type: {SERVER_TYPE}")
    return False


# Keep connect_plex as an alias so existing call sites still work
connect_plex = connect_server

def get_smart_categories_for_movies():
    """Generate smart categories for movies using PlexClient."""
    categories = []
    base_id    = 10000

    if not media_client:
        return categories

    try:
        movie_sections = [s for s in get_cached_sections() if s['type'] == 'movie']

        for section in movie_sections:
            lib_id    = section['id']
            lib_title = section['title']

            # Recently Added
            categories.append({
                'id': str(base_id), 'name': f"🆕 Recently Added - {lib_title}",
                'type': 'plex_recently_added', 'section_id': lib_id, 'limit': 50
            })
            base_id += 1

            # Genres
            for genre in media_client.get_genres(lib_id, 'movie'):
                categories.append({
                    'id': str(base_id), 'name': f"🎭 {genre} - {lib_title}",
                    'type': 'plex_genre', 'section_id': lib_id, 'genre': genre, 'limit': 200
                })
                base_id += 1

            # Decades
            for decade in media_client.get_decades(lib_id, 'movie'):
                categories.append({
                    'id': str(base_id), 'name': f"📅 {decade}s - {lib_title}",
                    'type': 'plex_decade', 'section_id': lib_id, 'decade': decade, 'limit': 200
                })
                base_id += 1

            # Collections
            for col in media_client.get_collections(lib_id):
                categories.append({
                    'id': str(base_id), 'name': f"📚 {col['title']}",
                    'type': 'plex_collection', 'section_id': lib_id,
                    'collection_id': col['id'], 'limit': 200
                })
                base_id += 1

    except Exception as e:
        print(f"Error generating movie categories: {e}")

    return categories

def get_smart_categories_for_series():
    """Generate smart categories for TV shows using PlexClient."""
    categories = []
    base_id    = 20000

    if not media_client:
        return categories

    try:
        tv_sections = [s for s in get_cached_sections() if s['type'] == 'show']

        for section in tv_sections:
            lib_id    = section['id']
            lib_title = section['title']

            # Recently Added
            categories.append({
                'id': str(base_id), 'name': f"🆕 Recently Added - {lib_title}",
                'type': 'plex_recently_added', 'section_id': lib_id, 'limit': 50
            })
            base_id += 1

            # Genres
            for genre in media_client.get_genres(lib_id, 'show'):
                categories.append({
                    'id': str(base_id), 'name': f"🎭 {genre} - {lib_title}",
                    'type': 'plex_genre', 'section_id': lib_id, 'genre': genre, 'limit': 200
                })
                base_id += 1

            # Decades (1950+)
            for decade in media_client.get_decades(lib_id, 'show'):
                if decade >= 1950:
                    categories.append({
                        'id': str(base_id), 'name': f"📅 {decade}s - {lib_title}",
                        'type': 'plex_decade', 'section_id': lib_id, 'decade': decade, 'limit': 200
                    })
                    base_id += 1

            # Collections
            for col in media_client.get_collections(lib_id):
                categories.append({
                    'id': str(base_id), 'name': f"📚 {col['title']}",
                    'type': 'plex_collection', 'section_id': lib_id,
                    'collection_id': col['id'], 'limit': 200
                })
                base_id += 1

    except Exception as e:
        print(f"Error generating series categories: {e}")

    return categories

def get_movies_for_category(category):
    """Get movies for a specific category using PlexClient."""
    movies = []
    if not media_client:
        return movies

    lib_id = category['section_id']
    limit  = category.get('limit', 200)

    try:
        cat_type = category['type']
        if cat_type == 'plex_recently_added':
            items = media_client.get_recently_added(lib_id, limit)
            items = [i for i in items if i['type'] == 'movie']
        elif cat_type == 'plex_unwatched':
            items = media_client.get_unwatched_movies(lib_id, limit)
        elif cat_type == 'plex_genre':
            items = media_client.get_by_genre(lib_id, category['genre'], 'movie', limit)
        elif cat_type == 'plex_decade':
            items = media_client.get_by_decade(lib_id, category['decade'], 'movie', limit)
        elif cat_type == 'plex_collection':
            items = media_client.get_collection_items(category['collection_id'], 'movie')[:limit]
        else:
            items = []

        for item in items:
            formatted = format_movie_for_xtream(item, category['id'])
            if formatted:
                movies.append(formatted)

    except Exception as e:
        print(f"Error getting movies for category: {e}")

    return movies


def get_series_for_category(category):
    """Get TV shows for a specific category using PlexClient."""
    series = []
    if not media_client:
        return series

    lib_id = category['section_id']
    limit  = category.get('limit', 200)

    try:
        cat_type = category['type']
        if cat_type == 'plex_recently_added':
            items = media_client.get_recently_added(lib_id, limit)
            items = [i for i in items if i['type'] == 'show']
        elif cat_type == 'plex_unwatched':
            items = media_client.get_unwatched_shows(lib_id, limit)
        elif cat_type == 'plex_genre':
            items = media_client.get_by_genre(lib_id, category['genre'], 'show', limit)
        elif cat_type == 'plex_decade':
            items = media_client.get_by_decade(lib_id, category['decade'], 'show', limit)
        elif cat_type == 'plex_collection':
            items = media_client.get_collection_items(category['collection_id'], 'show')[:limit]
        else:
            items = []

        for item in items:
            formatted = format_series_for_xtream(item, category['id'])
            if formatted:
                series.append(formatted)

    except Exception as e:
        print(f"Error getting series for category: {e}")

    return series

# ─────────────────────────────────────────────────────────────────────────────
# Continue Watching / On Deck
# ─────────────────────────────────────────────────────────────────────────────

def get_on_deck_movies(limit=None):
    """Return in-progress movies from On Deck using PlexClient."""
    if not media_client:
        return []

    max_items = limit or ON_DECK_LIMIT
    movies    = []
    seen      = set()

    try:
        for item in media_client.get_on_deck(max_items * 2):
            if len(movies) >= max_items:
                break
            if item['type'] != 'movie':
                continue
            if item['id'] in seen:
                continue
            seen.add(item['id'])

            formatted = format_movie_for_xtream(item, ON_DECK_MOVIE_CAT_ID)
            if formatted:
                view_offset = item.get('view_offset', 0) or 0
                duration    = item.get('duration', 0) or 0
                if duration > 0:
                    formatted['progress']      = round(view_offset / duration * 100)
                    formatted['view_offset']   = view_offset // 1000
                    formatted['duration_secs'] = duration    // 1000
                movies.append(formatted)
    except Exception as e:
        print(f"[ON_DECK] Error fetching movies: {e}")

    print(f"[ON_DECK] Returning {len(movies)} in-progress movies")
    return movies


def get_on_deck_series(limit=None):
    """Return TV shows with in-progress episodes from On Deck using PlexClient."""
    if not media_client:
        return []

    max_items = limit or ON_DECK_LIMIT
    series    = []
    seen      = set()

    try:
        for item in media_client.get_on_deck(max_items * 2):
            if len(series) >= max_items:
                break
            if item['type'] != 'episode':
                continue

            show = media_client.get_show_for_episode(item)
            if not show:
                continue

            if show['id'] in seen:
                continue
            seen.add(show['id'])

            formatted = format_series_for_xtream(show, ON_DECK_SERIES_CAT_ID)
            if formatted:
                formatted['next_episode_title']  = item.get('title', '')
                formatted['next_episode_season'] = item.get('season_number')
                formatted['next_episode_num']    = item.get('episode_number')
                view_offset = item.get('view_offset', 0) or 0
                duration    = item.get('duration', 0) or 0
                if duration > 0:
                    formatted['next_episode_progress'] = round(view_offset / duration * 100)
                series.append(formatted)
    except Exception as e:
        print(f"[ON_DECK] Error fetching series: {e}")

    print(f"[ON_DECK] Returning {len(series)} in-progress TV shows")
    return series


# Load config and connect on startup
load_config()
connect_server()
load_category_filters()

# Session storage
sessions = {}
active_streams = {}  # Track active streaming sessions

# ─────────────────────────────────────────────────────────────────────────────
# Stats tracking
# ─────────────────────────────────────────────────────────────────────────────

_stats_lock = threading.Lock()

bridge_stats = {
    'start_time':        time.time(),
    'total_requests':    0,
    'requests_by_action': {},
    'total_streams':     0,
    'streams_by_type':   {},
    'recent_activity':   [],
    'tmdb_cache_hits':   0,
    'tmdb_cache_misses': 0,
}


def _record_request(action, username):
    """Record an API request in the in-memory stats."""
    with _stats_lock:
        bridge_stats['total_requests'] += 1
        bridge_stats['requests_by_action'][action] = \
            bridge_stats['requests_by_action'].get(action, 0) + 1

        bridge_stats['recent_activity'].append({
            'time':   datetime.now().strftime('%H:%M:%S'),
            'action': action,
            'user':   username or 'unknown'
        })
        # Keep only the last 50 events
        if len(bridge_stats['recent_activity']) > 50:
            bridge_stats['recent_activity'] = bridge_stats['recent_activity'][-50:]


def _record_stream(stream_type, title=''):
    """Record a stream start in stats."""
    with _stats_lock:
        bridge_stats['total_streams'] += 1
        bridge_stats['streams_by_type'][stream_type] = \
            bridge_stats['streams_by_type'].get(stream_type, 0) + 1


def _record_tmdb_lookup(hit: bool):
    """Record a TMDb cache hit or miss."""
    with _stats_lock:
        if hit:
            bridge_stats['tmdb_cache_hits'] += 1
        else:
            bridge_stats['tmdb_cache_misses'] += 1


def _get_uptime_str():
    """Return a human-readable uptime string."""
    secs = int(time.time() - bridge_stats['start_time'])
    days, rem  = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if days:  parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if mins:  parts.append(f"{mins}m")
    parts.append(f"{secs}s")
    return ' '.join(parts)


def _get_live_stats():
    """Assemble the full stats payload for the dashboard."""
    cleanup_inactive_streams()

    cached_movies = len(session_cache.get('movies', {}))
    cached_series = len(session_cache.get('series', {}))

    total_movies = 0
    total_shows  = 0
    try:
        if media_client:
            for section in get_cached_sections():
                if section['type'] == 'movie':
                    total_movies += media_client.total_view_size(section['id'], 'movie')
                elif section['type'] == 'show':
                    total_shows  += media_client.total_view_size(section['id'], 'show')
    except Exception:
        pass

    active = []
    for stream in active_streams.values():
        try:
            item  = media_client.get_item(stream['stream_id']) if media_client else None
            title = item['title'] if item else f"ID {stream['stream_id']}"
        except Exception:
            title = f"ID {stream['stream_id']}"
        active.append({
            'user':     stream['username'],
            'title':    title,
            'type':     stream['stream_type'],
            'started':  datetime.fromtimestamp(stream['started_at']).strftime('%H:%M:%S'),
            'duration': int((time.time() - stream['started_at']) / 60)
        })

    with _stats_lock:
        hits          = bridge_stats['tmdb_cache_hits']
        misses        = bridge_stats['tmdb_cache_misses']
        total_lookups = hits + misses
        hit_rate      = round(hits / total_lookups * 100) if total_lookups > 0 else None

        return {
            'uptime':             _get_uptime_str(),
            'total_requests':     bridge_stats['total_requests'],
            'requests_by_action': dict(sorted(bridge_stats['requests_by_action'].items(), key=lambda x: x[1], reverse=True)),
            'total_streams':      bridge_stats['total_streams'],
            'streams_by_type':    bridge_stats['streams_by_type'],
            'active_streams':     active,
            'active_user_count':  len(set(s['username'] for s in active_streams.values())),
            'cached_movies':      cached_movies,
            'cached_series':      cached_series,
            'total_movies':       total_movies,
            'total_shows':        total_shows,
            'tmdb_enabled':       bool(TMDB_API_KEY),
            'tmdb_cache_hits':    hits,
            'tmdb_cache_misses':  misses,
            'tmdb_total_lookups': total_lookups,
            'tmdb_hit_rate':      hit_rate,
            'recent_activity':    list(reversed(bridge_stats['recent_activity'])),
        }

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
    _record_stream(stream_type)
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
    """Return stream URL from a normalized item dict."""
    if media_client:
        return media_client.get_stream_url(item)
    return None


def format_movie_for_xtream(item, category_id=1, skip_tmdb=False):
    """Format a normalized movie dict to Xtream Codes format."""
    try:
        stream_url = get_stream_url(item)
        if not stream_url:
            return None

        # TMDb enrichment with caching
        tmdb_data = None
        if TMDB_API_KEY and not skip_tmdb:
            cache_key = f"movie_{item['id']}"
            if cache_key in session_cache['movies']:
                tmdb_data = session_cache['movies'][cache_key]
                _record_tmdb_lookup(hit=True)
            else:
                _record_tmdb_lookup(hit=False)
                tmdb_data = enhance_movie_with_tmdb(item)
                if tmdb_data:
                    session_cache['movies'][cache_key] = tmdb_data

        poster_url   = (tmdb_data or {}).get('poster_path')   or item.get('thumb', '')
        backdrop_url = (tmdb_data or {}).get('backdrop_path') or item.get('art', '')
        year_str     = str(item['year']) if item.get('year') else ''

        return {
            "stream_id":          item['id'],
            "num":                item['id'],
            "name":               item.get('title', ''),
            "stream_icon":        poster_url,
            "icon":               poster_url,
            "cover":              poster_url,
            "poster":             poster_url,
            "image":              poster_url,
            "cover_big":          backdrop_url,
            "backdrop":           backdrop_url,
            "fanart":             backdrop_url,
            "added":              str(item.get('added_at', '')),
            "category_id":        str(category_id),
            "container_extension": item.get('media_parts', [{}])[0].get('container', 'mkv'),
            "direct_source":      stream_url,
            "year":               year_str,
            "releaseDate":        year_str,
            "tmdb_matched":       bool(tmdb_data),
        }
    except Exception as e:
        print(f"[FORMAT] Error formatting movie: {e}")
        return None


def format_series_for_xtream(item, category_id=2):
    """Format a normalized show dict to Xtream Codes format."""
    try:
        # TMDb enrichment with caching
        tmdb_data = None
        if TMDB_API_KEY:
            cache_key = f"series_{item['id']}"
            if cache_key in session_cache['series']:
                tmdb_data = session_cache['series'][cache_key]
                _record_tmdb_lookup(hit=True)
            else:
                _record_tmdb_lookup(hit=False)
                tmdb_data = enhance_series_with_tmdb(item)
                if tmdb_data:
                    session_cache['series'][cache_key] = tmdb_data

        poster_url   = (tmdb_data or {}).get('poster_path')   or item.get('thumb', '')
        backdrop_url = (tmdb_data or {}).get('backdrop_path') or item.get('art', '')
        year_str     = str(item['year']) if item.get('year') else ''
        rating       = item.get('rating') or 0

        return {
            "series_id":    item['id'],
            "num":          item['id'],
            "name":         item.get('title', ''),
            "cover":        poster_url,
            "poster":       poster_url,
            "image":        poster_url,
            "icon":         poster_url,
            "cover_big":    backdrop_url,
            "backdrop":     backdrop_url,
            "fanart":       backdrop_url,
            "plot":         item.get('summary', ''),
            "cast":         ", ".join(item.get('roles', [])),
            "director":     ", ".join(item.get('directors', [])),
            "genre":        ", ".join(item.get('genres', [])),
            "releaseDate":  year_str,
            "year":         year_str,
            "rating":       str(rating),
            "rating_5based": round(float(rating) / 2, 1),
            "backdrop_path": [backdrop_url] if backdrop_url else [],
            "category_id":  str(category_id),
            "tmdb_matched": bool(tmdb_data),
        }
    except Exception as e:
        print(f"[FORMAT] Error formatting series: {e}")
        return None


def format_episode_for_xtream(item, series_id):
    """Format a normalized episode dict to Xtream Codes format."""
    try:
        stream_url = get_stream_url(item)
        if not stream_url:
            return None

        duration = item.get('duration', 0) or 0
        return {
            "id":                  item['id'],
            "episode_num":         item.get('episode_number'),
            "title":               item.get('title', ''),
            "container_extension": item.get('media_parts', [{}])[0].get('container', 'mkv'),
            "info": {
                "tmdb_id":      "",
                "releasedate":  item.get('air_date', ''),
                "plot":         item.get('summary', ''),
                "duration_secs": str(duration // 1000),
                "duration":      str(duration // 60000),
                "rating":        str(item.get('rating', '0') or '0'),
                "season":        item.get('season_number'),
                "cover_big":     item.get('thumb', ''),
            },
            "direct_source": stream_url,
        }
    except Exception as e:
        print(f"[FORMAT] Error formatting episode: {e}")
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
            <p>Connect your Plex, Emby, or Jellyfin library to any Xtream UI player</p>
        </div>
        
        <div class="status-card">
            <h2 style="margin-bottom: 20px;">System Status</h2>
            <div class="status-grid">
                <div class="status-item">
                    <h3>Media Server</h3>
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
                    <h3 style="margin-bottom: 10px; color: #333; font-size: 16px;">1️⃣ Configure Media Server Connection</h3>
                    <p style="color: #666; margin-bottom: 10px;">Go to Settings and enter your media server URL and token.</p>
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
                <a href="/admin/stats" class="button">📊 Stats</a>
                <a href="/admin/categories" class="button">📋 Categories</a>
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
                <h2>Media Server</h2>

                <div class="form-group">
                    <label for="server_type">Server Type</label>
                    <select id="server_type" name="server_type" onchange="showServerFields(this.value)"
                            style="width:100%;padding:12px;border:2px solid #e1e4e8;border-radius:8px;font-size:14px;">
                        <option value="plex"      {% if server_type == 'plex'      %}selected{% endif %}>Plex</option>
                        <option value="jellyfin"  {% if server_type == 'jellyfin'  %}selected{% endif %}>Jellyfin</option>
                        <option value="emby"      {% if server_type == 'emby'      %}selected{% endif %}>Emby</option>
                    </select>
                </div>

                <!-- Plex fields -->
                <div id="plex-fields">
                    <div class="form-group">
                        <label for="plex_url">Plex Server URL</label>
                        <input type="text" id="plex_url" name="plex_url" value="{{ plex_url }}" placeholder="http://192.168.1.100:32400">
                        <small>Include http:// or https://</small>
                    </div>
                    <div class="form-group">
                        <label for="plex_token">Plex Token</label>
                        <input type="text" id="plex_token" name="plex_token" value="{{ plex_token }}" placeholder="Your Plex authentication token">
                        <small>Plex Web → Play media → Get Info → View XML → Copy X-Plex-Token</small>
                    </div>
                </div>

                <!-- Emby / Jellyfin fields -->
                <div id="emby-fields" style="display:none;">
                    <div class="form-group">
                        <label for="emby_url">Server URL</label>
                        <input type="text" id="emby_url" name="emby_url" value="{{ emby_url }}" placeholder="http://192.168.1.100:8096">
                        <small>Include http:// or https:// and the port</small>
                    </div>
                    <div class="form-group">
                        <label for="emby_api_key">API Key</label>
                        <input type="text" id="emby_api_key" name="emby_api_key" value="{{ emby_api_key }}" placeholder="Your API key">
                        <small>Dashboard → Advanced → API Keys → New API Key</small>
                    </div>
                    <div class="form-group">
                        <label for="emby_user_id">User ID</label>
                        <div style="display:flex;gap:8px;align-items:flex-start;">
                            <div style="flex:1;">
                                <input type="text" id="emby_user_id" name="emby_user_id" value="{{ emby_user_id }}" placeholder="User ID (GUID)">
                                <small>The user whose watch history and library is used</small>
                            </div>
                            <button type="button" onclick="discoverUsers()"
                                    style="padding:12px 16px;background:#28a745;color:white;border:none;border-radius:8px;cursor:pointer;white-space:nowrap;font-size:13px;">
                                🔍 Discover
                            </button>
                        </div>
                        <div id="user-list" style="margin-top:8px;display:none;border:1px solid #e1e4e8;border-radius:8px;overflow:hidden;"></div>
                    </div>
                </div>

                <div class="form-group">
                    <label for="tmdb_api_key">TMDb API Key (Optional)</label>
                    <input type="text" id="tmdb_api_key" name="tmdb_api_key" value="{{ tmdb_api_key }}" placeholder="Your TMDb API key for enhanced metadata">
                    <small>Get free API key from <a href="https://www.themoviedb.org/settings/api" target="_blank">themoviedb.org/settings/api</a></small>
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

<script>
function showServerFields(type) {
    document.getElementById('plex-fields').style.display  = (type === 'plex')  ? '' : 'none';
    document.getElementById('emby-fields').style.display  = (type !== 'plex')  ? '' : 'none';
}

function discoverUsers() {
    const url     = document.getElementById('emby_url').value.trim();
    const api_key = document.getElementById('emby_api_key').value.trim();
    const flavour = document.getElementById('server_type').value;
    const list    = document.getElementById('user-list');

    if (!url || !api_key) {
        alert('Enter the server URL and API key first.');
        return;
    }

    list.style.display = 'block';
    list.innerHTML     = '<div style="padding:10px;color:#666;">Discovering users…</div>';

    fetch(`/admin/discover-users?url=${encodeURIComponent(url)}&api_key=${encodeURIComponent(api_key)}&flavour=${flavour}`)
        .then(r => r.json())
        .then(d => {
            if (d.success && d.users.length > 0) {
                list.innerHTML = d.users.map(u =>
                    `<div onclick="selectUser('${u.id}','${u.name}')"
                          style="padding:10px 14px;cursor:pointer;border-bottom:1px solid #f0f0f0;font-size:14px;"
                          onmouseover="this.style.background='#f8f9fa'" onmouseout="this.style.background=''">
                        <strong>${u.name}</strong>
                        <span style="color:#999;font-size:12px;margin-left:8px;">${u.id}</span>
                    </div>`
                ).join('');
            } else {
                list.innerHTML = `<div style="padding:10px;color:#dc3545;">${d.error || 'No users found'}</div>`;
            }
        })
        .catch(e => {
            list.innerHTML = `<div style="padding:10px;color:#dc3545;">Error: ${e}</div>`;
        });
}

function selectUser(id, name) {
    document.getElementById('emby_user_id').value = id;
    document.getElementById('user-list').style.display = 'none';
}

// Show correct fields on page load
showServerFields(document.getElementById('server_type').value);
</script>
</body>
</html>
"""

STATS_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Stats - Plex Xtream Bridge</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        .card {
            background: white;
            padding: 25px;
            border-radius: 15px;
            box-shadow: 0 5px 20px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }
        .card h1 { color: #333; margin-bottom: 5px; }
        .card h2 { color: #333; margin-bottom: 20px; font-size: 18px; }
        .subtitle { color: #666; margin-bottom: 20px; }
        .stat-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 15px;
            margin-bottom: 10px;
        }
        .stat-box {
            background: #f8f9fa;
            border-left: 4px solid #667eea;
            border-radius: 8px;
            padding: 18px;
        }
        .stat-box.green  { border-color: #28a745; }
        .stat-box.orange { border-color: #fd7e14; }
        .stat-box.red    { border-color: #dc3545; }
        .stat-box.teal   { border-color: #20c997; }
        .stat-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: #888; margin-bottom: 6px; }
        .stat-value { font-size: 28px; font-weight: 700; color: #333; }
        .stat-sub   { font-size: 12px; color: #999; margin-top: 4px; }
        table { width: 100%; border-collapse: collapse; }
        th { text-align: left; padding: 10px 12px; background: #f8f9fa; color: #555; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 2px solid #e9ecef; }
        td { padding: 10px 12px; border-bottom: 1px solid #f0f0f0; color: #444; font-size: 14px; }
        tr:last-child td { border-bottom: none; }
        tr:hover td { background: #fafafa; }
        .badge { display: inline-block; padding: 3px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
        .badge-movie  { background: #cce5ff; color: #004085; }
        .badge-series { background: #d4edda; color: #155724; }
        .badge-episode { background: #d4edda; color: #155724; }
        .bar-wrap { background: #e9ecef; border-radius: 4px; height: 8px; margin-top: 6px; }
        .bar-fill { background: #667eea; border-radius: 4px; height: 8px; }
        .button { display: inline-block; padding: 10px 20px; background: #667eea; color: white; text-decoration: none; border-radius: 8px; font-weight: 600; font-size: 14px; }
        .button-secondary { background: #6c757d; }
        .refresh-note { font-size: 12px; color: #999; float: right; margin-top: 4px; }
        .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        @media (max-width: 700px) { .two-col { grid-template-columns: 1fr; } }
        .empty { color: #aaa; font-style: italic; padding: 20px 0; text-align: center; }
    </style>
</head>
<body>
<div class="container">

    <div class="card">
        <h1>📊 Bridge Stats</h1>
        <p class="subtitle">Live statistics — auto-refreshes every 10 seconds</p>
        <a href="/admin" class="button button-secondary">← Dashboard</a>
        <span class="refresh-note" id="last-refresh">Refreshing...</span>
    </div>

    <!-- Key numbers -->
    <div class="card">
        <h2>Overview</h2>
        <div class="stat-grid" id="overview-grid">
            <div class="stat-box green">
                <div class="stat-label">Uptime</div>
                <div class="stat-value" id="s-uptime">{{ uptime }}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">Total API Requests</div>
                <div class="stat-value" id="s-requests">{{ total_requests }}</div>
            </div>
            <div class="stat-box orange">
                <div class="stat-label">Total Streams Started</div>
                <div class="stat-value" id="s-streams">{{ total_streams }}</div>
                <div class="stat-sub">
                    {% for stype, count in streams_by_type.items() %}
                    {{ stype }}: {{ count }}{% if not loop.last %} · {% endif %}
                    {% endfor %}
                </div>
            </div>
            <div class="stat-box teal">
                <div class="stat-label">Active Viewers</div>
                <div class="stat-value" id="s-active">{{ active_user_count }}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">Movies in Library</div>
                <div class="stat-value">{{ total_movies }}</div>
                <div class="stat-sub">{{ cached_movies }} TMDb cached</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">Shows in Library</div>
                <div class="stat-value">{{ total_shows }}</div>
                <div class="stat-sub">{{ cached_series }} TMDb cached</div>
            </div>
            <div class="stat-box {% if tmdb_hit_rate is not none %}{% if tmdb_hit_rate >= 80 %}green{% elif tmdb_hit_rate >= 50 %}orange{% else %}red{% endif %}{% endif %}">
                <div class="stat-label">TMDb Cache Hit Rate</div>
                <div class="stat-value" id="s-hitrate">
                    {% if tmdb_hit_rate is not none %}{{ tmdb_hit_rate }}%{% else %}—{% endif %}
                </div>
                <div class="stat-sub" id="s-hitrate-sub">
                    {% if tmdb_total_lookups > 0 %}
                    {{ tmdb_cache_hits }} hits · {{ tmdb_cache_misses }} misses · {{ tmdb_total_lookups }} total
                    {% else %}
                    No lookups yet
                    {% endif %}
                </div>
            </div>
        </div>
    </div>

    <div class="two-col">
        <!-- Active streams -->
        <div class="card">
            <h2>▶ Active Streams</h2>
            <div id="active-streams-table">
            {% if active_streams %}
            <table>
                <tr><th>User</th><th>Title</th><th>Type</th><th>Duration</th></tr>
                {% for s in active_streams %}
                <tr>
                    <td>{{ s.user }}</td>
                    <td>{{ s.title }}</td>
                    <td><span class="badge badge-{{ s.type }}">{{ s.type }}</span></td>
                    <td>{{ s.duration }}m</td>
                </tr>
                {% endfor %}
            </table>
            {% else %}
            <div class="empty">No active streams</div>
            {% endif %}
            </div>
        </div>

        <!-- Recent activity -->
        <div class="card">
            <h2>🕐 Recent Activity</h2>
            <div id="recent-activity-table">
            {% if recent_activity %}
            <table>
                <tr><th>Time</th><th>Action</th><th>User</th></tr>
                {% for ev in recent_activity[:15] %}
                <tr>
                    <td style="font-family:monospace;font-size:12px;">{{ ev.time }}</td>
                    <td style="font-size:12px;">{{ ev.action }}</td>
                    <td style="font-size:12px;">{{ ev.user }}</td>
                </tr>
                {% endfor %}
            </table>
            {% else %}
            <div class="empty">No activity yet</div>
            {% endif %}
            </div>
        </div>
    </div>

    <!-- Requests by action -->
    <div class="card">
        <h2>📡 Requests by Action</h2>
        {% if requests_by_action %}
        {% set max_count = requests_by_action.values() | max %}
        <table>
            <tr><th>Action</th><th>Count</th><th style="width:40%">Share</th></tr>
            {% for action, count in requests_by_action.items() %}
            <tr>
                <td style="font-family:monospace;font-size:13px;">{{ action }}</td>
                <td>{{ count }}</td>
                <td>
                    <div class="bar-wrap">
                        <div class="bar-fill" style="width:{{ (count / max_count * 100) | int }}%"></div>
                    </div>
                </td>
            </tr>
            {% endfor %}
        </table>
        {% else %}
        <div class="empty">No requests recorded yet</div>
        {% endif %}
    </div>

</div>

<script>
function refreshStats() {
    fetch('/admin/stats/data')
        .then(r => r.json())
        .then(d => {
            document.getElementById('s-uptime').textContent   = d.uptime;
            document.getElementById('s-requests').textContent = d.total_requests;
            document.getElementById('s-streams').textContent  = d.total_streams;
            document.getElementById('s-active').textContent   = d.active_user_count;

            // TMDb hit rate
            const hr    = document.getElementById('s-hitrate');
            const hrSub = document.getElementById('s-hitrate-sub');
            if (d.tmdb_hit_rate !== null) {
                hr.textContent    = d.tmdb_hit_rate + '%';
                hrSub.textContent = `${d.tmdb_cache_hits} hits · ${d.tmdb_cache_misses} misses · ${d.tmdb_total_lookups} total`;
                const box = hr.closest('.stat-box');
                box.classList.remove('green', 'orange', 'red');
                box.classList.add(d.tmdb_hit_rate >= 80 ? 'green' : d.tmdb_hit_rate >= 50 ? 'orange' : 'red');
            } else {
                hr.textContent    = '—';
                hrSub.textContent = 'No lookups yet';
            }

            // Active streams table
            const at = document.getElementById('active-streams-table');
            if (d.active_streams.length === 0) {
                at.innerHTML = '<div class="empty">No active streams</div>';
            } else {
                at.innerHTML = '<table><tr><th>User</th><th>Title</th><th>Type</th><th>Duration</th></tr>' +
                    d.active_streams.map(s =>
                        `<tr><td>${s.user}</td><td>${s.title}</td>` +
                        `<td><span class="badge badge-${s.type}">${s.type}</span></td>` +
                        `<td>${s.duration}m</td></tr>`
                    ).join('') + '</table>';
            }

            // Recent activity
            const ra = document.getElementById('recent-activity-table');
            if (d.recent_activity.length === 0) {
                ra.innerHTML = '<div class="empty">No activity yet</div>';
            } else {
                ra.innerHTML = '<table><tr><th>Time</th><th>Action</th><th>User</th></tr>' +
                    d.recent_activity.slice(0, 15).map(e =>
                        `<tr><td style="font-family:monospace;font-size:12px;">${e.time}</td>` +
                        `<td style="font-size:12px;">${e.action}</td>` +
                        `<td style="font-size:12px;">${e.user}</td></tr>`
                    ).join('') + '</table>';
            }

            document.getElementById('last-refresh').textContent =
                'Last refresh: ' + new Date().toLocaleTimeString();
        })
        .catch(() => {
            document.getElementById('last-refresh').textContent = 'Refresh failed';
        });
}

// Refresh every 10 seconds
setInterval(refreshStats, 10000);
refreshStats();
</script>
</body>
</html>
"""

CATEGORIES_FILTER_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Categories - Plex Xtream Bridge</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh; padding: 20px;
        }
        .container { max-width: 1100px; margin: 0 auto; }
        .card {
            background: white; padding: 25px; border-radius: 15px;
            box-shadow: 0 5px 20px rgba(0,0,0,0.1); margin-bottom: 20px;
        }
        .card h1 { color: #333; margin-bottom: 5px; }
        .card h2 { color: #333; margin-bottom: 15px; font-size: 18px; }
        .subtitle { color: #666; margin-bottom: 20px; font-size: 14px; }
        .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        @media (max-width: 750px) { .two-col { grid-template-columns: 1fr; } }
        .section-header {
            display: flex; justify-content: space-between; align-items: center;
            margin-bottom: 12px; padding-bottom: 10px;
            border-bottom: 2px solid #f0f0f0;
        }
        .section-header h3 { color: #444; font-size: 15px; }
        .select-links { font-size: 12px; }
        .select-links a {
            color: #667eea; text-decoration: none; cursor: pointer; margin-left: 8px;
        }
        .select-links a:hover { text-decoration: underline; }
        .cat-list { max-height: 340px; overflow-y: auto; border: 1px solid #e9ecef; border-radius: 8px; }
        .cat-item {
            display: flex; align-items: center; gap: 10px;
            padding: 9px 14px; border-bottom: 1px solid #f5f5f5;
            transition: background 0.1s;
        }
        .cat-item:last-child { border-bottom: none; }
        .cat-item:hover { background: #fafafa; }
        .cat-item label {
            flex: 1; font-size: 14px; color: #333; cursor: pointer;
            display: flex; align-items: center; gap: 8px;
        }
        .cat-item input[type=checkbox] { width: 16px; height: 16px; cursor: pointer; accent-color: #667eea; }
        .badge-special {
            font-size: 10px; background: #667eea; color: white;
            padding: 2px 6px; border-radius: 8px; white-space: nowrap;
        }
        .badge-smart {
            font-size: 10px; background: #e9ecef; color: #666;
            padding: 2px 6px; border-radius: 8px; white-space: nowrap;
        }
        .count-pill {
            font-size: 11px; color: #999; background: #f0f0f0;
            padding: 2px 7px; border-radius: 10px; white-space: nowrap;
        }
        .info-box {
            background: #e7f3ff; border-left: 4px solid #2196F3;
            padding: 12px 15px; border-radius: 5px; margin-bottom: 18px;
            font-size: 13px; color: #0c5460;
        }
        .alert { padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; font-size: 14px; }
        .alert-success { background: #d4edda; color: #155724; border-left: 4px solid #28a745; }
        .alert-error   { background: #f8d7da; color: #721c24; border-left: 4px solid #dc3545; }
        .btn {
            display: inline-block; padding: 11px 22px; border-radius: 8px;
            font-weight: 600; border: none; cursor: pointer; font-size: 14px;
            text-decoration: none; transition: opacity 0.2s;
        }
        .btn:hover { opacity: 0.88; }
        .btn-primary { background: #667eea; color: white; }
        .btn-secondary { background: #6c757d; color: white; }
        .btn-success { background: #28a745; color: white; }
        .footer-bar {
            position: sticky; bottom: 0; background: white;
            padding: 15px 25px; border-top: 1px solid #e9ecef;
            display: flex; align-items: center; gap: 12px;
            box-shadow: 0 -4px 12px rgba(0,0,0,0.06); border-radius: 0 0 15px 15px;
        }
        #save-msg { font-size: 13px; color: #666; }
        .loading { text-align: center; padding: 40px; color: #aaa; font-size: 14px; }
        .enabled-count { font-size: 13px; color: #667eea; font-weight: 600; }
    </style>
</head>
<body>
<div class="container">

    <div class="card">
        <h1>📋 Category Filters</h1>
        <p class="subtitle">Choose which categories are relayed to your IPTV player. All categories are opt-in.</p>
        <a href="/admin" class="btn btn-secondary" style="font-size:13px;padding:8px 16px;">← Dashboard</a>
    </div>

    <div id="alert-area"></div>

    <div class="card">
        <div class="info-box">
            Check the categories you want visible in your player, then click <strong>Save Changes</strong>.
            Unchecked categories will be hidden from your player completely.
            Smart categories (genres, decades, collections) are discovered automatically from your library.
        </div>

        <div class="two-col" id="cat-grid">
            <div class="loading">Loading categories…</div>
        </div>

        <div class="footer-bar">
            <button class="btn btn-primary" onclick="saveChanges()">💾 Save Changes</button>
            <button class="btn btn-secondary" onclick="location.reload()">↺ Reset</button>
            <span id="save-msg"></span>
        </div>
    </div>

</div>

<script>
let state = { movies: { special: [], smart: [] }, series: { special: [], smart: [] } };

function buildSection(mediaType, label, icon) {
    const data    = state[mediaType];
    const allCats = [...data.special, ...data.smart];
    const enabled = allCats.filter(c => c.enabled).length;

    const specialRows = data.special.map(c => catRow(c, mediaType, 'special', 'special')).join('');
    const smartRows   = data.smart.map(c =>   catRow(c, mediaType, 'smart',   'smart')).join('');

    return `
    <div>
        <h2>${icon} ${label}</h2>
        <p class="enabled-count" id="count-${mediaType}">${enabled} of ${allCats.length} enabled</p>

        <div class="section-header" style="margin-top:14px;">
            <h3>Special</h3>
            <span class="select-links">
                <a onclick="selectAll('${mediaType}','special')">All</a>
                <a onclick="selectNone('${mediaType}','special')">None</a>
            </span>
        </div>
        <div class="cat-list" id="list-${mediaType}-special">${specialRows}</div>

        <div class="section-header" style="margin-top:18px;">
            <h3>Smart <span class="count-pill">${data.smart.length}</span></h3>
            <span class="select-links">
                <a onclick="selectAll('${mediaType}','smart')">All</a>
                <a onclick="selectNone('${mediaType}','smart')">None</a>
            </span>
        </div>
        <div class="cat-list" id="list-${mediaType}-smart">${smartRows}</div>
    </div>`;
}

function catRow(cat, mediaType, bucket, badgeType) {
    const chk   = cat.enabled ? 'checked' : '';
    const badge = badgeType === 'special'
        ? '<span class="badge-special">special</span>'
        : '<span class="badge-smart">smart</span>';
    return `
    <div class="cat-item">
        <label>
            <input type="checkbox" ${chk}
                onchange="toggle('${mediaType}','${bucket}','${cat.id}',this.checked)">
            ${cat.name}
        </label>
        ${badge}
    </div>`;
}

function toggle(mediaType, bucket, id, enabled) {
    const cat = state[mediaType][bucket].find(c => c.id === id);
    if (cat) cat.enabled = enabled;
    updateCount(mediaType);
}

function updateCount(mediaType) {
    const all     = [...state[mediaType].special, ...state[mediaType].smart];
    const enabled = all.filter(c => c.enabled).length;
    const el      = document.getElementById('count-' + mediaType);
    if (el) el.textContent = enabled + ' of ' + all.length + ' enabled';
}

function selectAll(mediaType, bucket) {
    state[mediaType][bucket].forEach(c => c.enabled = true);
    render(); 
}

function selectNone(mediaType, bucket) {
    state[mediaType][bucket].forEach(c => c.enabled = false);
    render();
}

function render() {
    document.getElementById('cat-grid').innerHTML =
        buildSection('movies', 'Movies', '🎬') +
        buildSection('series', 'Series', '📺');
}

function showAlert(msg, type) {
    document.getElementById('alert-area').innerHTML =
        `<div class="card"><div class="alert alert-${type}">${msg}</div></div>`;
    setTimeout(() => document.getElementById('alert-area').innerHTML = '', 4000);
}

function saveChanges() {
    const msg = document.getElementById('save-msg');
    msg.textContent = 'Saving…';
    fetch('/admin/categories/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(state)
    })
    .then(r => r.json())
    .then(d => {
        if (d.success) {
            msg.textContent = '✓ ' + d.message;
            showAlert('✅ ' + d.message, 'success');
        } else {
            msg.textContent = '✗ Error';
            showAlert('❌ ' + (d.error || 'Unknown error'), 'error');
        }
    })
    .catch(e => {
        msg.textContent = '✗ Error';
        showAlert('❌ ' + e, 'error');
    });
}

// Load on page open
fetch('/admin/categories/data')
    .then(r => r.json())
    .then(d => { state = d; render(); })
    .catch(() => {
        document.getElementById('cat-grid').innerHTML =
            '<div class="loading">Failed to load categories. Is Plex connected?</div>';
    });
</script>
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

@app.route('/admin/stats/data')
@require_admin_login
def stats_data():
    """JSON endpoint for live stats — polled by the dashboard every 10s."""
    return jsonify(_get_live_stats())


@app.route('/admin/stats')
@require_admin_login
def admin_stats():
    """Stats dashboard page."""
    stats = _get_live_stats()
    return render_template_string(STATS_HTML, **stats)


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
    
    unmatched_movies = []
    unmatched_shows  = []

    if media_client:
        for section in get_cached_sections():
            if section['type'] == 'movie':
                for item in media_client.get_all_movies(section['id']):
                    cache_key = f"movie_{item['id']}"
                    if cache_key not in session_cache['movies']:
                        unmatched_movies.append({'id': item['id'], 'title': item.get('title', ''), 'year': item.get('year', '')})
            elif section['type'] == 'show':
                for item in media_client.get_all_shows(section['id']):
                    cache_key = f"series_{item['id']}"
                    if cache_key not in session_cache['series']:
                        unmatched_shows.append({'id': item['id'], 'title': item.get('title', ''), 'year': item.get('year', '')})
    
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
        for section in get_cached_sections():
            if section['type'] != ('movie' if content_type == 'movie' else 'show'):
                continue
            items = media_client.search(section['id'], query, content_type) if media_client else []
            for item in items[:20]:
                cache_key      = f"{'movie' if content_type == 'movie' else 'series'}_{item['id']}"
                cache_category = 'movies' if content_type == 'movie' else 'series'
                results.append({
                    'id':      item['id'],
                    'title':   item.get('title', ''),
                    'year':    item.get('year', ''),
                    'matched': cache_key in session_cache[cache_category]
                })
                if len(results) >= 20:
                    break
            if len(results) >= 20:
                break

        return jsonify({"results": results})
    except Exception as e:
        print(f"[ERROR] Search error: {e}")
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
    libraries   = []
    server_name = "Not connected"

    if media_client:
        server_name = media_client.server_name
        for section in get_cached_sections():
            try:
                count = media_client.total_view_size(section['id'], section['type'])
            except Exception:
                count = 0
            libraries.append({
                'name':  section['title'],
                'type':  section['type'].title(),
                'count': count
            })

    import socket
    hostname  = socket.gethostname()
    local_ip  = socket.gethostbyname(hostname)
    bridge_url = f"http://{local_ip}:{BRIDGE_PORT}"

    return render_template_string(DASHBOARD_HTML,
        plex_connected=media_client is not None,
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
    """Settings page — supports Plex, Emby, and Jellyfin."""
    global PLEX_URL, PLEX_TOKEN, SERVER_TYPE, EMBY_URL, EMBY_API_KEY, EMBY_USER_ID
    global BRIDGE_USERNAME, BRIDGE_PASSWORD, ADMIN_PASSWORD, SHOW_DUMMY_CHANNEL, TMDB_API_KEY

    message = None
    error   = False

    if request.method == 'POST':
        new_server_type = request.form.get('server_type', 'plex').strip()
        new_plex_url    = request.form.get('plex_url', '').strip()
        new_plex_token  = request.form.get('plex_token', '').strip()
        new_emby_url    = request.form.get('emby_url', '').strip()
        new_emby_key    = request.form.get('emby_api_key', '').strip()
        new_emby_uid    = request.form.get('emby_user_id', '').strip()
        new_bridge_user = request.form.get('bridge_username', '').strip()
        new_bridge_pass = request.form.get('bridge_password', '').strip()
        new_admin_pass  = request.form.get('admin_password', '').strip()
        new_tmdb_key    = request.form.get('tmdb_api_key', '').strip()
        new_show_dummy  = request.form.get('show_dummy_channel') == 'on'

        if not new_bridge_user or not new_bridge_pass:
            message, error = "✗ Bridge username and password cannot be empty", True
        elif not new_admin_pass:
            message, error = "✗ Admin password cannot be empty", True
        else:
            SERVER_TYPE        = new_server_type
            PLEX_URL           = new_plex_url
            PLEX_TOKEN         = new_plex_token
            EMBY_URL           = new_emby_url
            EMBY_API_KEY       = new_emby_key
            EMBY_USER_ID       = new_emby_uid
            BRIDGE_USERNAME    = new_bridge_user
            BRIDGE_PASSWORD    = new_bridge_pass
            ADMIN_PASSWORD     = new_admin_pass
            TMDB_API_KEY       = new_tmdb_key
            SHOW_DUMMY_CHANNEL = new_show_dummy

            if save_config():
                if connect_server():
                    message = f"✓ Settings saved and connected to {SERVER_TYPE.title()} successfully!"
                else:
                    message = f"⚠️ Settings saved but failed to connect to {SERVER_TYPE.title()}. Check your credentials."
                    error   = True
            else:
                message, error = "✗ Failed to save settings", True

    return render_template_string(SETTINGS_HTML,
        server_type=SERVER_TYPE,
        plex_url=PLEX_URL,
        plex_token=PLEX_TOKEN,
        emby_url=EMBY_URL,
        emby_api_key=EMBY_API_KEY,
        emby_user_id=EMBY_USER_ID,
        bridge_username=BRIDGE_USERNAME,
        bridge_password=BRIDGE_PASSWORD,
        admin_password=ADMIN_PASSWORD,
        tmdb_api_key=TMDB_API_KEY,
        show_dummy_channel=SHOW_DUMMY_CHANNEL,
        message=message,
        error=error
    )


@app.route('/admin/discover-users')
@require_admin_login
def discover_users():
    """Return user list from Emby/Jellyfin for the setup UI."""
    url     = request.args.get('url', '').strip()
    api_key = request.args.get('api_key', '').strip()
    flavour = request.args.get('flavour', 'jellyfin')

    if not url or not api_key:
        return jsonify({'success': False, 'error': 'URL and API key required'}), 400

    try:
        client = EmbyJellyfinClient(url, api_key, '', flavour)
        users  = client.get_users()
        return jsonify({'success': True, 'users': users})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/admin/test')
@require_admin_login
def admin_test():
    """Test media server connection."""
    if connect_server():
        return jsonify({
            "success":   True,
            "message":   f"Successfully connected to {media_client.server_name if media_client else 'server'}",
            "version":   "",
            "libraries": [s['title'] for s in get_cached_sections()]
        })
    return jsonify({
        "success": False,
        "message": f"Failed to connect to {SERVER_TYPE.title()}. Check your credentials."
    }), 500


# Xtream Codes API Endpoints (keeping all previous API code)

@app.route('/player_api.php')
def player_api():
    """Main Xtream Codes API endpoint"""
    if not media_client:
        return jsonify({"error": "Media server not connected"}), 500
    
    action = request.args.get('action')
    username = request.args.get('username')
    
    # Log the request (helpful for debugging)
    if action:
        print(f"[API] Request: action={action}, user={username}")
        _record_request(action, username)
    
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
                # ── Continue Watching (movies) ──────────────────────────────
                try:
                    has_movie_deck = any(
                        i['type'] == 'movie'
                        for i in (media_client.get_on_deck() if media_client else [])
                    )
                except Exception:
                    has_movie_deck = False

                if has_movie_deck and is_category_enabled(ON_DECK_MOVIE_CAT_ID, 'movies'):
                    categories.append({
                        "category_id":   ON_DECK_MOVIE_CAT_ID,
                        "category_name": "▶ Continue Watching",
                        "parent_id": 0
                    })

                # ── Unwatched Movies ────────────────────────────────────────
                if is_category_enabled(UNWATCHED_MOVIE_CAT_ID, 'movies'):
                    categories.append({
                        "category_id":   UNWATCHED_MOVIE_CAT_ID,
                        "category_name": "🎬 Unwatched Movies",
                        "parent_id": 0
                    })

                # ── Regular library sections ────────────────────────────────
                sections = get_cached_sections()
                for section in sections:
                    if section['type'] == 'movie':
                        categories.append({
                            "category_id":   str(section['id']),
                            "category_name": section["title"],
                            "parent_id": 0
                        })

                # ── Smart categories (genre, decade, collections) ───────────
                for cat in get_smart_categories_for_movies():
                    if is_category_enabled(cat['id'], 'movies'):
                        categories.append({
                            "category_id":   cat['id'],
                            "category_name": cat['name'],
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

        # ── Continue Watching (movies) ─────────────────────────────────────
        if category_id == ON_DECK_MOVIE_CAT_ID:
            movies = get_on_deck_movies(limit if limit > 0 else None)
            elapsed = time.time() - start_time
            print(f"[PERF] Returned {len(movies)} on-deck movies in {elapsed:.2f}s")
            return jsonify(movies)

        # ── Unwatched Movies ───────────────────────────────────────────────
        if category_id == UNWATCHED_MOVIE_CAT_ID:
            try:
                max_limit = limit if limit > 0 else MAX_MOVIES
                for section in get_cached_sections():
                    if section['type'] == 'movie' and media_client:
                        for movie in media_client.get_unwatched_movies(section['id'], max_limit):
                            formatted = format_movie_for_xtream(movie, UNWATCHED_MOVIE_CAT_ID)
                            if formatted:
                                movies.append(formatted)
            except Exception as e:
                print(f"[ERROR] Unwatched movies: {e}")
            elapsed = time.time() - start_time
            print(f"[PERF] Returned {len(movies)} unwatched movies in {elapsed:.2f}s")
            return jsonify(movies)

        # Handle "All Movies" category (category_id = "0")
        if category_id == "0" or not category_id:
            max_limit = limit if limit > 0 else MAX_MOVIES
            count     = 0
            for section in get_cached_sections():
                if section['type'] == 'movie' and media_client:
                    try:
                        for movie in media_client.get_all_movies(section['id']):
                            if count >= max_limit:
                                break
                            formatted = format_movie_for_xtream(movie, section['id'])
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
                        section = next((s for s in get_cached_sections() if str(s["id"]) == str(category_id)), None)
                        if section and media_client:
                            all_movies = media_client.get_all_movies(section['id'])
                            movies_to_process = all_movies[:limit] if limit > 0 else all_movies
                            for movie in movies_to_process:
                                formatted = format_movie_for_xtream(movie, category_id)
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
            movie = media_client.get_item(vod_id) if media_client else None
            if not movie:
                return jsonify({"error": "Item not found"}), 404
            stream_url = get_stream_url(movie)

            tmdb_data = enhance_movie_with_tmdb(movie) if TMDB_API_KEY else {}

            info = {
                "info": {
                    "tmdb_id":        str(tmdb_data.get('tmdb_id', '')) if tmdb_data else "",
                    "imdb_id":        tmdb_data.get('imdb_id', '') if tmdb_data else "",
                    "name":           movie.get('title', ''),
                    "o_name":         movie.get('original_title', movie.get('title', '')),
                    "cover_big":      tmdb_data.get('backdrop_path', '') if tmdb_data else movie.get('art', ''),
                    "movie_image":    tmdb_data.get('poster_path', '') if tmdb_data else movie.get('thumb', ''),
                    "releasedate":    str(movie.get('year', '')) if movie.get('year') else "",
                    "youtube_trailer": tmdb_data.get('trailer', '') if tmdb_data else "",
                    "director":       tmdb_data.get('director', '') if tmdb_data else ", ".join(movie.get('directors', [])),
                    "actors":         ', '.join([c['name'] for c in tmdb_data.get('cast', [])]) if tmdb_data and tmdb_data.get('cast') else ", ".join(movie.get('roles', [])),
                    "cast":           ', '.join([c['name'] for c in tmdb_data.get('cast', [])]) if tmdb_data and tmdb_data.get('cast') else ", ".join(movie.get('roles', [])),
                    "description":    tmdb_data.get('overview', '') if tmdb_data else movie.get('summary', ''),
                    "plot":           tmdb_data.get('overview', '') if tmdb_data else movie.get('summary', ''),
                    "age":            movie.get('content_rating', ''),
                    "rating":         str(tmdb_data.get('vote_average', movie.get('rating') or 0)),
                    "rating_5based":  tmdb_data.get('vote_average', round(float(movie.get('rating') or 0) / 2, 1)),
                    "duration_secs":  str((movie.get('duration') or 0) // 1000),
                    "duration":       str((movie.get('duration') or 0) // 60000),
                    "genre":          ', '.join(tmdb_data.get('genres', [])) if tmdb_data else ", ".join(movie.get('genres', [])),
                    "backdrop_path":  [tmdb_data['backdrop_path']] if tmdb_data and tmdb_data.get('backdrop_path') else ([movie.get('art', '')] if movie.get('art') else []),
                    "popularity":     tmdb_data.get('popularity', 0) if tmdb_data else 0,
                    "vote_count":     tmdb_data.get('vote_count', 0) if tmdb_data else 0,
                    "tagline":        tmdb_data.get('tagline', '') if tmdb_data else "",
                    "keywords":       ', '.join(tmdb_data.get('keywords', [])) if tmdb_data else "",
                },
                "movie_data": {
                    "stream_id":          movie['id'],
                    "name":               movie.get('title', ''),
                    "container_extension": movie.get('media_parts', [{}])[0].get('container', 'mkv'),
                    "custom_sid":         "",
                    "direct_source":      stream_url
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
                # ── Continue Watching (TV shows) ────────────────────────────
                try:
                    has_episode_deck = any(
                        i['type'] == 'episode'
                        for i in (media_client.get_on_deck() if media_client else [])
                    )
                except Exception:
                    has_episode_deck = False

                if has_episode_deck and is_category_enabled(ON_DECK_SERIES_CAT_ID, 'series'):
                    categories.append({
                        "category_id":   ON_DECK_SERIES_CAT_ID,
                        "category_name": "▶ Continue Watching",
                        "parent_id": 0
                    })

                # ── Unwatched Shows ─────────────────────────────────────────
                if is_category_enabled(UNWATCHED_SERIES_CAT_ID, 'series'):
                    categories.append({
                        "category_id":   UNWATCHED_SERIES_CAT_ID,
                        "category_name": "📺 Unwatched Shows",
                        "parent_id": 0
                    })

                # ── Regular library sections ────────────────────────────────
                sections = get_cached_sections()
                for section in sections:
                    if section['type'] == 'show':
                        categories.append({
                            "category_id":   str(section['id']),
                            "category_name": section["title"],
                            "parent_id": 0
                        })

                # ── Smart categories ────────────────────────────────────────
                for cat in get_smart_categories_for_series():
                    if is_category_enabled(cat['id'], 'series'):
                        categories.append({
                            "category_id":   cat['id'],
                            "category_name": cat['name'],
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

        # ── Continue Watching (TV shows) ───────────────────────────────────
        if category_id == ON_DECK_SERIES_CAT_ID:
            series_list = get_on_deck_series(limit if limit > 0 else None)
            elapsed = time.time() - start_time
            print(f"[PERF] Returned {len(series_list)} on-deck series in {elapsed:.2f}s")
            return jsonify(series_list)

        # ── Unwatched Shows ────────────────────────────────────────────────
        if category_id == UNWATCHED_SERIES_CAT_ID:
            try:
                max_limit = limit if limit > 0 else MAX_SHOWS
                for section in get_cached_sections():
                    if section['type'] == 'show' and media_client:
                        for show in media_client.get_unwatched_shows(section['id'], max_limit):
                            formatted = format_series_for_xtream(show, UNWATCHED_SERIES_CAT_ID)
                            if formatted:
                                series_list.append(formatted)
            except Exception as e:
                print(f"[ERROR] Unwatched series: {e}")
            elapsed = time.time() - start_time
            print(f"[PERF] Returned {len(series_list)} unwatched shows in {elapsed:.2f}s")
            return jsonify(series_list)

        # Handle "All Series" category (category_id = "0")
        if category_id == "0" or not category_id:
            max_limit = limit if limit > 0 else MAX_SHOWS
            print(f"[DEBUG] Returning all series from all sections (max {max_limit})")
            count = 0
            for section in get_cached_sections():
                if section['type'] == 'show' and media_client:
                    print(f"[DEBUG] Processing TV section: {section['title']}")
                    try:
                        for show in media_client.get_all_shows(section['id']):
                            if count >= max_limit:
                                break
                            formatted = format_series_for_xtream(show, section['id'])
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
                        section = next((s for s in get_cached_sections() if str(s["id"]) == str(category_id)), None)
                        if section and media_client:
                            print(f"[DEBUG] Using section: {section['title']}")
                            all_shows = media_client.get_all_shows(section['id'])
                            shows_to_process = all_shows[:limit] if limit > 0 else all_shows
                            for show in shows_to_process:
                                formatted = format_series_for_xtream(show, category_id)
                                if formatted:
                                    series_list.append(formatted)
                    except Exception as e:
                        print(f"[ERROR] Error getting series: {e}")
        else:
            # No category specified - return all series
            max_limit = limit if limit > 0 else MAX_SHOWS
            print(f"[DEBUG] Returning all series from all sections (max {max_limit})")
            count = 0

            for section in get_cached_sections():
                if section['type'] == 'show':
                    print(f"[DEBUG] Processing TV section: {section['title']}")
                    try:
                        for show in media_client.get_all_shows(section['id']):
                            if count >= max_limit:
                                break
                            formatted = format_series_for_xtream(show, section['id'])
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
            for section in get_cached_sections():
                if section['type'] == 'livetv':
                    has_plex_livetv = True
                    categories.append({
                        "category_id":   section['id'],
                        "category_name": section["title"],
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
            for section in get_cached_sections():
                if section['type'] == 'livetv':
                    has_plex_livetv = True
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
                movie_count = sum(1 for s in get_cached_sections() if s.type == 'movie')
                show_count = sum(1 for s in get_cached_sections() if s.type == 'show')
                
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

            show = media_client.get_item(series_id) if media_client else None
            if not show:
                return jsonify({"seasons": [], "info": {"name": f"Series {series_id}", "cover": "", "plot": "Unable to load series information", "cast": "", "director": "", "genre": "", "releaseDate": "", "rating": "0", "rating_5based": 0, "backdrop_path": [], "youtube_trailer": "", "episode_run_time": "", "category_id": "2"}, "episodes": {}})

            print(f"[DEBUG] Getting series info for: {show.get('title')} (type: {show.get('type')})")

            if show.get('type') != 'show':
                return jsonify({"error": f"Item is not a TV show, it's a {show.get('type')}"}), 400

            seasons       = []
            episodes_data = {}

            for season in media_client.get_seasons(series_id):
                season_num = season.get('season_number')
                if season_num is None:
                    print(f"[DEBUG] Skipping season with no number")
                    continue

                print(f"[DEBUG] Processing season {season_num}: {season.get('title')}")

                ep_list = media_client.get_episodes(season['id'])
                season_info = {
                    "air_date":      str(season.get('year', '')),
                    "episode_count": len(ep_list),
                    "id":            season['id'],
                    "name":          season.get('title', ''),
                    "overview":      season.get('summary', ''),
                    "season_number": season_num,
                    "cover":         season.get('thumb', ''),
                    "cover_big":     season.get('art', ''),
                }
                seasons.append(season_info)

                episodes = []
                for episode in ep_list:
                    formatted_episode = format_episode_for_xtream(episode, series_id)
                    if formatted_episode:
                        episodes.append(formatted_episode)

                print(f"[DEBUG] Season {season_num} has {len(episodes)} episodes")
                if episodes:
                    episodes_data[str(season_num)] = episodes

            print(f"[DEBUG] Total seasons: {len(seasons)}, Total episode groups: {len(episodes_data)}")

            rating = show.get('rating') or 0
            info = {
                "seasons": seasons,
                "info": {
                    "name":             show.get('title', ''),
                    "cover":            show.get('thumb', ''),
                    "plot":             show.get('summary', ''),
                    "cast":             ", ".join(show.get('roles', [])),
                    "director":         ", ".join(show.get('directors', [])),
                    "genre":            ", ".join(show.get('genres', [])),
                    "releaseDate":      str(show.get('year', '')) if show.get('year') else "",
                    "rating":           str(rating),
                    "rating_5based":    round(float(rating) / 2, 1),
                    "backdrop_path":    [show.get('art', '')] if show.get('art') else [],
                    "youtube_trailer":  "",
                    "episode_run_time": "",
                    "category_id":      "2"
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
        movie = media_client.get_item(stream_id) if media_client else None
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
        episode = media_client.get_item(stream_id) if media_client else None
        if episode:
            print(f"[STREAM] Found episode: {episode.get('title')} (S{episode.get('season_number')}E{episode.get('episode_number')})")
        stream_url = get_stream_url(episode) if episode else None
        if stream_url:
            print(f"[STREAM] Redirecting to: {stream_url[:100]}...")
            return Response(status=302, headers={'Location': stream_url})
        print(f"[ERROR] No stream URL generated for episode {stream_id}")
        return "Stream not found", 404
    except Exception as e:
        print(f"[ERROR] Error streaming episode {stream_id}: {e}")
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
            section = next((s for s in get_cached_sections() if str(s["id"]) == str(category_id)), None)
            category = {
                'id': category_id,
                'name': f"📁 {section["title"]}",
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
            try:
                section = next((s for s in get_cached_sections() if str(s["id"]) == str(category_id)), None)
                items = media_client.get_all_movies(section['id']) if section and media_client else []
            except:
                items = []
        else:
            items = get_movies_for_category(category)
    else:
        if category['type'] == 'plex_library':
            try:
                section = next((s for s in get_cached_sections() if str(s["id"]) == str(category_id)), None)
                items = media_client.get_all_shows(section['id']) if section and media_client else []
            except:
                items = []
        else:
            items = get_series_for_category(category)
    
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
    for item in items[:200]:
        try:
            title  = item.get('title', '') if isinstance(item, dict) else str(item)
            year   = item.get('year', 'N/A') if isinstance(item, dict) else 'N/A'
            rating = item.get('rating', 'N/A') if isinstance(item, dict) else 'N/A'
            poster = item.get('thumb', '') if isinstance(item, dict) else ''
            
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
    """Category filter management page."""
    return render_template_string(CATEGORIES_FILTER_HTML)


@app.route('/admin/categories/data')
@require_admin_login
def categories_data():
    """JSON endpoint — returns full category list with enabled state."""
    return jsonify(get_full_category_state())


@app.route('/admin/categories/save', methods=['POST'])
@require_admin_login
def categories_save():
    """Save category filter selections from the UI."""
    global category_filters
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data received'}), 400

        # Rebuild filters from submitted state
        new_filters = {
            'movies': {'special': {}, 'smart': {}},
            'series': {'special': {}, 'smart': {}}
        }

        for media_type in ('movies', 'series'):
            for bucket in ('special', 'smart'):
                for item in data.get(media_type, {}).get(bucket, []):
                    cat_id  = str(item.get('id', ''))
                    enabled = bool(item.get('enabled', False))
                    if cat_id:
                        new_filters[media_type][bucket][cat_id] = enabled

        category_filters = new_filters
        save_category_filters()

        movie_count  = sum(1 for v in new_filters['movies']['special'].values() if v)
        movie_count += sum(1 for v in new_filters['movies']['smart'].values()   if v)
        series_count  = sum(1 for v in new_filters['series']['special'].values() if v)
        series_count += sum(1 for v in new_filters['series']['smart'].values()   if v)

        return jsonify({
            'success': True,
            'message': f'Saved — {movie_count} movie and {series_count} series categories enabled'
        })
    except Exception as e:
        print(f"[CATEGORIES] Error saving filters: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


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
